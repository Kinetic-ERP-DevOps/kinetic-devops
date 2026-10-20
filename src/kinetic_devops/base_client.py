"""
kinetic_devops/base_client.py

KineticBaseClient - Session management and authenticated API requests.

Inherits security infrastructure and wire logging from KineticCore.
Provides:
- Interactive environment/user selection
- Automatic token management via keyring
- Header generation with API key and company context
- Generic request execution for custom endpoints
"""

# File: kinetic_devops/base_client.py
from kinetic_devops import _lazy_import

requests = _lazy_import("requests")
import sys
import os
import base64
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from urllib.parse import urlparse
from .auth import KineticConfigManager
from .KineticCore import KineticCore  # Import your new centralized brain [cite: 2]
from .fs_ops import describe_overwrite_risk, is_write_permitted, required_force_flag

class KineticBaseClient(KineticCore):
    """
    The General Infrastructure Layer.
    Inherits Header Generation and Wire Logging from KineticCore. [cite: 2]
    """
    def __init__(self, env_nickname: Optional[str] = None, user_id: Optional[str] = None, company_id: Optional[str] = None, debug: bool = False):
        # Initialize the Core with debug preference [cite: 2]
        super().__init__(debug=debug)
        self.mgr = KineticConfigManager()

        env_nickname = env_nickname or os.environ.get("KINETIC_SESSION_ENV")
        user_id = user_id or os.environ.get("KINETIC_SESSION_USER")
        company_id = company_id or os.environ.get("KINETIC_SESSION_CO")
        
        # Standardized environment/user selection 
        if not env_nickname:
            self.env_name, self.user_id, self.active_co = self.mgr.prompt_for_env()
        else:
            self.env_name = env_nickname
            self.user_id = user_id
            self.active_co = company_id

        # Authenticate and get the full config [cite: 121]
        # We pass self.active_co to ensure we don't lose the selection from prompt_for_env
        config_data = self.mgr.get_active_config((self.env_name, self.user_id, self.active_co),
                                                fields=("url", "token", "api_key", "company", "nickname", "user_id"))
        
        if not config_data[0]: # Check if URL exists [cite: 121]
            print(f"❌ Failed to initialize session for {self.user_id}@{self.env_name}")
            sys.exit(1)
            
        self.config = {
            "url": config_data[0],
            "token": config_data[1],
            "api_key": config_data[2],
            "company": config_data[3],
            "nickname": config_data[4],
            "user_id": config_data[5],
        }
        self.configure_file_resolution(
            conflict_resolution="timestamp",
            force=False,
            force_low=False,
            force_medium=False,
            force_high=False,
            force_critical=False,
            no_force_low=False,
            no_force_none=False,
            confirm_overwrite=True,
            warn_on_drift=True,
        )

    @staticmethod
    def add_file_resolution_args(parser: Any) -> None:
        parser.add_argument(
            "--file-conflict",
            choices=["timestamp", "increment", "overwrite", "error"],
            default="timestamp",
            help="What to do when an output file already exists: timestamp = write a new timestamped copy "
                 "next to it, increment = add a numeric suffix, overwrite = replace it (subject to the "
                 "--force-* risk checks and an OVERWRITE confirmation), error = abort (default: timestamp).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="With --file-conflict overwrite: allow overwriting at every risk level (skips the "
                 "--force-* checks below).",
        )
        parser.add_argument(
            "--force-low",
            action="store_true",
            help="Allow overwriting LOW risk files (git-tracked, recoverable from Git); allowed by default "
                 "unless --no-force-low is given.",
        )
        parser.add_argument(
            "--force-medium",
            action="store_true",
            help="Allow overwriting MEDIUM risk files (untracked, git-ignored by an extension rule).",
        )
        parser.add_argument(
            "--force-high",
            action="store_true",
            help="Allow overwriting HIGH risk files (untracked, git-ignored by a path/directory rule).",
        )
        parser.add_argument(
            "--force-critical",
            action="store_true",
            help="Allow overwriting CRITICAL risk files (untracked and not ignored, or outside any Git repo; "
                 "no recovery possible).",
        )
        parser.add_argument(
            "--no-force-low",
            action="store_true",
            help="Block overwriting LOW risk (git-tracked) files unless --force-low is also given.",
        )
        parser.add_argument(
            "--no-force-none",
            action="store_true",
            help="Block overwrites even for paths classified as NONE risk.",
        )
        parser.add_argument(
            "--no-confirm-overwrite",
            action="store_true",
            help="Skip the 'type OVERWRITE' prompt in overwrite mode (required for non-interactive runs).",
        )
        parser.add_argument(
            "--no-drift-warning",
            action="store_true",
            help="Suppress warnings about tracked-file drift and untracked overwrite loss.",
        )

    def configure_file_resolution(
        self,
        conflict_resolution: str = "timestamp",
        force: bool = False,
        force_low: bool = False,
        force_medium: bool = False,
        force_high: bool = False,
        force_critical: bool = False,
        no_force_low: bool = False,
        no_force_none: bool = False,
        confirm_overwrite: bool = True,
        warn_on_drift: bool = True,
    ) -> None:
        self._file_conflict_resolution = conflict_resolution
        self._file_force = bool(force)
        self._file_force_low = bool(force_low)
        self._file_force_medium = bool(force_medium)
        self._file_force_high = bool(force_high)
        self._file_force_critical = bool(force_critical)
        self._file_no_force_low = bool(no_force_low)
        self._file_no_force_none = bool(no_force_none)
        self._file_confirm_overwrite = bool(confirm_overwrite)
        self._file_warn_on_drift = bool(warn_on_drift)

    def configure_file_resolution_from_args(self, args: Any) -> None:
        self.configure_file_resolution(
            conflict_resolution=getattr(args, "file_conflict", "timestamp"),
            force=bool(getattr(args, "force", False)),
            force_low=bool(getattr(args, "force_low", False)),
            force_medium=bool(getattr(args, "force_medium", False)),
            force_high=bool(getattr(args, "force_high", False)),
            force_critical=bool(getattr(args, "force_critical", False)),
            no_force_low=bool(getattr(args, "no_force_low", False)),
            no_force_none=bool(getattr(args, "no_force_none", False)),
            confirm_overwrite=not bool(getattr(args, "no_confirm_overwrite", False)),
            warn_on_drift=not bool(getattr(args, "no_drift_warning", False)),
        )

    def _build_runtime_substitutions(self, plant: str = "") -> Dict[str, str]:
        """Build runtime substitutions for redacted/template dump placeholders."""
        parsed = urlparse(self.config["url"])
        instance = parsed.path.strip("/")
        mapping = {
            "HOSTNAME": parsed.hostname or "",
            "hostname": parsed.hostname or "",
            "INSTANCE": instance,
            "COMPANY": self.config.get("company", ""),
            "Company": self.config.get("company", ""),
            "COMPANYID": self.config.get("company", ""),
            "USER_ID": self.config.get("user_id", ""),
            "USERID": self.config.get("user_id", ""),
            "PLANT": plant,
        }
        return mapping

    @staticmethod
    def file_to_base64(file_path: str) -> str:
        """Read a local file and return an ASCII base64 payload string."""
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        with open(file_path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")

    def resolve_output_path(
        self,
        output_path: str,
        conflict_resolution: Optional[str] = None,
        force: Optional[bool] = None,
        confirm_overwrite: Optional[bool] = None,
        warn_on_drift: Optional[bool] = None,
    ) -> str:
        """
        Resolve filename conflicts when a file already exists.
        
        Args:
            output_path: Target file path
            conflict_resolution: Strategy - 'timestamp', 'overwrite', 'error', or 'increment'
        
        Returns:
            Resolved file path
        
        Raises:
            FileExistsError: If conflict_resolution='error' and file exists
        """
        strategy = conflict_resolution or getattr(self, "_file_conflict_resolution", "timestamp")
        eff_force = bool(getattr(self, "_file_force", False) if force is None else force)
        eff_force_low = bool(getattr(self, "_file_force_low", False))
        eff_force_medium = bool(getattr(self, "_file_force_medium", False))
        eff_force_high = bool(getattr(self, "_file_force_high", False))
        eff_force_critical = bool(getattr(self, "_file_force_critical", False))
        eff_no_force_low = bool(getattr(self, "_file_no_force_low", False))
        eff_no_force_none = bool(getattr(self, "_file_no_force_none", False))
        eff_confirm = bool(getattr(self, "_file_confirm_overwrite", True) if confirm_overwrite is None else confirm_overwrite)
        eff_warn = bool(getattr(self, "_file_warn_on_drift", True) if warn_on_drift is None else warn_on_drift)

        if not os.path.isfile(output_path):
            return output_path
        
        if strategy == "overwrite":
            risk = describe_overwrite_risk(output_path)
            risk_level = str(risk.get("risk_level", "none"))
            reason = str(risk.get("reason", ""))
            ignore_type = str(risk.get("ignore_type", ""))
            ignore_pattern = str(risk.get("ignore_pattern", ""))
            ignore_source = str(risk.get("ignore_source", ""))

            if eff_warn and risk.get("tracked"):
                print(
                    f"⚠️  LOW risk overwrite on tracked file (recoverable via Git): {output_path}",
                    file=sys.stderr,
                )

            if eff_warn and risk_level in {"medium", "high", "critical"}:
                details = f"risk={risk_level} reason={reason}"
                if ignore_type:
                    details += f" ignore_type={ignore_type}"
                if ignore_pattern:
                    details += f" pattern={ignore_pattern}"
                if ignore_source:
                    details += f" source={ignore_source}"
                print(
                    f"⚠️  Overwrite risk detected for {output_path}: {details}",
                    file=sys.stderr,
                )

            permitted, permit_reason = is_write_permitted(
                risk_level=risk_level,
                force=eff_force,
                force_low=eff_force_low,
                force_medium=eff_force_medium,
                force_high=eff_force_high,
                force_critical=eff_force_critical,
                no_force_low=eff_no_force_low,
                no_force_none=eff_no_force_none,
            )

            if not permitted:
                required_flag = required_force_flag(risk_level)
                flag_hint = f" Use {required_flag} to override." if required_flag else ""
                raise PermissionError(
                    "Overwrite blocked by granular safety policy. "
                    f"Path={output_path} risk={risk_level} reason={reason}. "
                    f"permission={permit_reason}.{flag_hint}"
                )

            if eff_confirm:
                if not (sys.stdin.isatty() and sys.stdout.isatty()):
                    raise PermissionError(
                        f"Overwrite confirmation required for existing file: {output_path}. "
                        "Use --no-confirm-overwrite (or run interactively)."
                    )
                answer = input(f"Type OVERWRITE to confirm replacing {output_path}: ").strip()
                if answer != "OVERWRITE":
                    raise PermissionError(f"Overwrite canceled for file: {output_path}")

            return output_path
        
        if strategy == "error":
            raise FileExistsError(f"Output file already exists: {output_path}")
        
        base, ext = os.path.splitext(output_path)
        
        if strategy == "timestamp":
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            candidate = f"{base}_{timestamp}{ext}"
            if not os.path.isfile(candidate):
                if eff_warn:
                    print(
                        f"⚠️  Drift warning: existing file preserved by writing timestamped copy: {candidate}",
                        file=sys.stderr,
                    )
                return candidate

            counter = 1
            while True:
                candidate = f"{base}_{timestamp}_{counter}{ext}"
                if not os.path.isfile(candidate):
                    if eff_warn:
                        print(
                            f"⚠️  Drift warning: existing file preserved by writing timestamped copy: {candidate}",
                            file=sys.stderr,
                        )
                    return candidate
                counter += 1
        
        if strategy == "increment":
            counter = 1
            while os.path.isfile(f"{base}_{counter}{ext}"):
                counter += 1
            candidate = f"{base}_{counter}{ext}"
            if eff_warn:
                print(
                    f"⚠️  Drift warning: existing file preserved by writing incremented copy: {candidate}",
                    file=sys.stderr,
                )
            return candidate
        
        raise ValueError(f"Unknown conflict_resolution strategy: {strategy}")

    def execute_request(self, method: str, url: str, payload: Any = None,
                        params: Optional[str] = None, extra_headers: Optional[Dict] = None,
                        company: Optional[str] = None) -> Dict:
        # Use centralized header builder from KineticCore
        headers = self.build_headers(
            token=self.config['token'],
            api_key=self.config['api_key'],
            company=company if company else self.config['company']
        )
        
        if extra_headers:
            headers.update(extra_headers)
        
        try:
            response = requests.request(
                method, url, json=payload, params=params, headers=headers, timeout=60
            )
            
            # Use centralized wire logger from KineticCore [cite: 6, 7]
            # This replaces the old _log_wire_details [cite: 121]
            self.log_wire(method, url, headers, body=payload, resp=response)
            
            # Update session 'last used' timestamp [cite: 28, 126]
            if response.status_code not in (401, 403, 500, 503):
                self.mgr.touch_from_headers(response.request.headers)

            response.raise_for_status()
            return response.json()

        except Exception:
            # Re-raise to let specific tool (BAQ/Meta) handle it [cite: 128]
            raise
