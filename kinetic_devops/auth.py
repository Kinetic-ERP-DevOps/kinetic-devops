# kinetic_devops/auth.py
"""
Kinetic Config Manager — Project-Centric, Zero-Knowledge Vault, and CI/CD Orchestration Model

This module is part of a modern, modular Kinetic SDK infrastructure designed for:

1. **Project-Centric Repository Model**
    - Each Kinetic feature (e.g., Project-Daily-Production) is a self-contained Git submodule, including its own MetaUI, Reports, BPM exports, and BAQs.
    - The SDK (this repo) acts as the host, providing API, deployment, and orchestration logic, while project submodules are the payloads.
    - Public/private separation: SDK core remains clean and trackable; business logic and sensitive assets live in private submodules.

2. **Secure Auth Layer (with Optional Zero-Knowledge Vault)**
    - Credentials are stored in the OS keyring (Windows Credential Manager) by default.
    - Optional "Vault" mode: User provides a personal salt/passphrase at runtime.
    - PBKDF2 + AES-256: Credentials are encrypted before storage, ensuring zero-knowledge privacy.
    - Even with full keyring access, secrets are unreadable without the user’s passphrase (in Vault mode).
    - CI/CD compatibility: In automated environments, secrets can be injected via environment variables, bypassing interactive prompts.

3. **Orchestration & CI/CD Workflow**
    - The SDK provides CLI tools to iterate through project submodules, detect changes (via hashing), and push updates to Kinetic environments.
    - Environment switching (Dev/UAT/Production) is managed by mapping nicknames to encrypted credentials in the vault.
    - The Auth layer checks for passphrase/API keys in environment variables first, supporting both local and CI/CD workflows.
    - Pre-flight validation (JSON schema, RDL linting) is performed before deployment.
    - Deployment scripts automate upload of .erp/.rdl files to Kinetic endpoints (e.g., Ice.BO.BpMethodSvc, System.ReportSvc).

This file is intentionally lightweight and designed for both interactive developer use and automated CI/CD pipelines. It provides secure, modular, and maintainable credential and config management for all Kinetic SDK operations.
"""

import sys
import json
import base64
import getpass
import textwrap
import argparse
from kinetic_devops import _lazy_import

keyring = _lazy_import("keyring")
requests = _lazy_import("requests")
import time
import hashlib
import re
import os
from datetime import timedelta

# Ensure UTF-8 encoding for Windows terminals to support emojis
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from typing import Any, Optional, Dict, Tuple

# Ensure UTF-8 encoding for Windows terminals to support emojis
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from urllib.parse import urlparse
if __package__:
    from .KineticCore import KineticCore
    from . import crypto
else:
    from kinetic_devops.KineticCore import KineticCore
    from kinetic_devops import crypto

SERVICE_API_KEY = "epicor-kinetic-apikey"
SERVICE_SERVERS = "epicor-kinetic-servers"
# Named AI server profiles for kinetic_devops.analysis. Non-secret settings
# for all profiles live under "profiles" ({"default": name, "profiles":
# {name: {base_url, api_style, model}}}); each server's API key lives under its
# own normalized base URL, so profiles on the same server share one key.
SERVICE_AI = "kinetic-devops-ai"
AI_API_STYLES = ("openai", "openwebui")
AI_DEFAULT_PROFILE = "default"
_AI_LEGACY_USER = "profile"  # single-profile format from before named profiles


def _normalize_ai_url(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def _validate_ai_url(url: str) -> str:
    """An absolute http(s) URL with a host and nothing that belongs elsewhere:
    credentials embedded as user:password@ would land in the non-secret profile
    record (and ai-show output), so they, queries and fragments are rejected."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError for a malformed port
    except ValueError as exc:
        raise ValueError(f"AI base URL is malformed: {exc}") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("AI base URL must be an absolute http:// or https:// URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("AI base URL must not contain credentials; store the key with ai-set instead")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ValueError("AI base URL must not contain a query string or fragment")
    return url


def _read_ai_store() -> Dict[str, Any]:
    raw = keyring.get_password(SERVICE_AI, "profiles")
    try:
        store = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        store = None
    if isinstance(store, dict) and isinstance(store.get("profiles"), dict):
        return store
    # Migrate the earlier single-profile record on read (written back on the next change).
    raw_legacy = keyring.get_password(SERVICE_AI, _AI_LEGACY_USER)
    try:
        legacy = json.loads(raw_legacy) if raw_legacy else None
    except (TypeError, ValueError):
        legacy = None
    if isinstance(legacy, dict) and legacy.get("base_url"):
        return {"default": AI_DEFAULT_PROFILE, "profiles": {AI_DEFAULT_PROFILE: legacy}}
    return {"default": "", "profiles": {}}


def _write_ai_store(store: Dict[str, Any]) -> None:
    keyring.set_password(SERVICE_AI, "profiles", json.dumps(store))
    try:
        keyring.delete_password(SERVICE_AI, _AI_LEGACY_USER)
    except Exception:
        pass


def list_ai_profiles() -> Tuple[str, Dict[str, Dict[str, Any]]]:
    """(default profile name, {name: profile}). Profiles never include keys."""
    store = _read_ai_store()
    return store.get("default") or "", dict(store.get("profiles") or {})


def get_ai_profile(name: Optional[str] = None) -> Dict[str, Any]:
    """Settings of the named profile (or the default one): base_url, api_style,
    model. {} if there is no such profile. Never includes the key."""
    default, profiles = list_ai_profiles()
    profile = profiles.get(name or default) or {}
    return dict(profile) if isinstance(profile, dict) else {}


def get_ai_api_key(base_url: str) -> str:
    """API key stored for this AI server URL, or "" if none."""
    url = _normalize_ai_url(base_url)
    return (keyring.get_password(SERVICE_AI, url) or "") if url else ""


def set_ai_profile(
    base_url: str,
    api_style: str = "openai",
    model: str = "",
    api_key: Optional[str] = None,
    name: str = AI_DEFAULT_PROFILE,
    make_default: bool = False,
) -> Dict[str, Any]:
    """Create or replace a named profile; api_key=None keeps any existing key
    for the URL. The first profile stored becomes the default."""
    url = _normalize_ai_url(base_url)
    name = str(name or "").strip()
    if not name or any(ch.isspace() for ch in name):
        raise ValueError("Profile name must be non-empty and contain no whitespace.")
    url = _validate_ai_url(url)
    if api_style not in AI_API_STYLES:
        raise ValueError(f"api_style must be one of {AI_API_STYLES}")
    profile = {"base_url": url, "api_style": api_style, "model": str(model or "").strip()}
    store = _read_ai_store()
    store["profiles"][name] = profile
    if make_default or not store.get("default") or store["default"] not in store["profiles"]:
        store["default"] = name
    _write_ai_store(store)
    if api_key:
        keyring.set_password(SERVICE_AI, url, api_key)
    return profile


def set_default_ai_profile(name: str) -> None:
    store = _read_ai_store()
    if name not in store["profiles"]:
        raise ValueError(f"No AI profile named '{name}'.")
    store["default"] = name
    _write_ai_store(store)


def clear_ai_profile(name: Optional[str] = None) -> bool:
    """Delete one named profile, or every profile when name is None. A server's
    key is deleted only when no remaining profile uses that URL. Returns True if
    anything was removed."""
    store = _read_ai_store()
    profiles = store["profiles"]
    targets = list(profiles) if name is None else ([name] if name in profiles else [])
    removed = False
    for target in targets:
        url = _normalize_ai_url(profiles.pop(target).get("base_url", ""))
        removed = True
        if url and not any(_normalize_ai_url(p.get("base_url", "")) == url for p in profiles.values()):
            try:
                keyring.delete_password(SERVICE_AI, url)
            except Exception:
                pass
    if store.get("default") not in profiles:
        store["default"] = next(iter(profiles), "")
    if profiles:
        _write_ai_store(store)
    else:
        for user in ("profiles", _AI_LEGACY_USER):
            try:
                keyring.delete_password(SERVICE_AI, user)
                removed = True
            except Exception:
                pass
    return removed
DEFAULT_TTL_SEC = 1200


class SessionResolutionError(RuntimeError):
    """Raised when an explicitly-named environment's session can't be resolved.

    This intentionally never falls back to a different environment -- callers
    asked for a specific one and get a loud failure instead of silent drift.
    """


class KineticConfigManager(KineticCore):
    """Manager for Kinetic server configs and token lifecycle stored in keyring.

    Responsibilities:
    - Read/write server config map from keyring
    - Store and retrieve an API key
    - Fetch and cache access tokens from the Kinetic TokenResource endpoint
    - Provide CLI-friendly helpers to print shell-export commands
    """
    def __init__(self, debug: bool = False):
        super().__init__(debug=debug)
        self.os_type = "windows" if sys.platform == "win32" else "unix"
        self.is_interactive = sys.stdout.isatty()

    def _get_config_hash(self) -> str:
        """Required by KineticCore: Detects if the config map has changed."""
        raw = keyring.get_password(SERVICE_SERVERS, "config")
        if not raw: return "empty"
        return hashlib.md5(raw.encode()).hexdigest()

    def _get_server_dict(self) -> Dict:
        """Load the saved servers map from the keyring.

        Returns an empty dict when nothing is stored yet.
        """
        raw = keyring.get_password(SERVICE_SERVERS, "config")
        if not raw:
            return {}

        # If it's an encrypted blob, attempt to decrypt using environment
        # variable `KINETIC_VAULT_PASSPHRASE` or prompt interactively.
        if crypto.is_encrypted_blob(raw):
            passphrase = os.environ.get("KINETIC_VAULT_PASSPHRASE")
            if not passphrase and sys.stdout.isatty():
                try:
                    passphrase = getpass.getpass("Vault passphrase: ")
                except Exception:
                    passphrase = None

            if not passphrase:
                raise RuntimeError("Vault is encrypted. Set KINETIC_VAULT_PASSPHRASE or run in interactive terminal to provide passphrase.")

            try:
                return crypto.decrypt_json(raw, passphrase)
            except Exception as e:
                raise RuntimeError(f"Failed to decrypt vault: {e}")

        # Legacy plain JSON fallback
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _find_env(self, servers: Dict, name: str) -> Tuple[Optional[str], Optional[Dict]]:
        """Case-insensitive lookup for a saved server configuration.

        Returns the stored key and its config dict, or (None, None) when not found.
        """
        search_name = name.lower()
        for k, v in servers.items():
            if str(k).lower() == search_name:
                return k, v
        return None, None

    def _get_token_key(self, nickname: str, user_id: str, api_key: str, base_url: str = "") -> str:
        """Derive a keyring namespace for a specific environment/user credential.

        This is not a secret, password hash, or credential verifier. It is a
        deterministic keyring namespace used only to isolate cached bearer-token
        records per environment, user, and API credential. Including the base URL
        and API key ensures a rotated key or a different Epicor server does not
        resolve the prior session slot, while the 12-character SHA-256 suffix
        gives a 48-bit namespace (enough for uniqueness without exposing the
        underlying secret in the slot name).
        """
        # Keep slot derivation aligned with existing persisted keyring records.
        # `base_url` is accepted for API compatibility but intentionally unused.
        user = str(user_id or "").lower()
        secret_context = f"{api_key}{user}"
        key_hash = hashlib.sha256(secret_context.encode("utf-8")).hexdigest()[:12]
        return f"{nickname}-{user}-{key_hash}"

    def _list_slots_for_env(self, nickname: str) -> list:
        """Generates all possible token slots for a given environment."""
        servers = self._get_server_dict()
        name, cfg = self._find_env(servers, nickname)
        if not cfg:
            return []

        # Generate slots for all known sessions in this environment.
        slots = []
        for user in cfg.get("sessions", []):
            slot = self._get_token_key(name, user, cfg['api_key'])
            slots.append(slot)
        return slots

    def _get_api_key(self, nickname: str) -> str:
        """Retrieves the API key for a given environment nickname."""
        servers = self._get_server_dict()
        name, cfg = self._find_env(servers, nickname)
        if not cfg:
            return ""
        return cfg.get('api_key', '')

    def _get_token_meta(self, token_slot: str) -> Optional[Dict]:
        """Retrieves metadata using the unique token_slot identifier."""
        raw_data = keyring.get_password(token_slot, "current_token")
        if not raw_data: return None

        # If token slot is encrypted, decrypt it with same passphrase logic
        if crypto.is_encrypted_blob(raw_data):
            passphrase = os.environ.get("KINETIC_VAULT_PASSPHRASE")
            if not passphrase and sys.stdout.isatty():
                try:
                    passphrase = getpass.getpass("Vault passphrase: ")
                except Exception:
                    passphrase = None
            if not passphrase:
                # Can't decrypt non-interactively; treat as missing
                return None
            try:
                data = crypto.decrypt_json(raw_data, passphrase)
            except Exception:
                return None
        else:
            try:
                data = json.loads(raw_data)
            except Exception:
                return None

        try:
            stored_at = data.get("_local_timestamp", 0)
            expires_in = data.get("expires_in") or data.get("ExpiresIn") or DEFAULT_TTL_SEC
            remaining = int(expires_in - (time.time() - stored_at))
            data["_remaining"] = remaining if remaining > 0 else 0
            data["_is_valid"] = remaining > 0
            return data
        except Exception:
            return None

    def get_session_by_bearer(self, token_to_match: str, fields: Tuple[str, ...]) -> Tuple:
        """
        Global reverse lookup. Identifies the session by scanning obscured slots.
        """
        if not token_to_match:
            return tuple(None for _ in fields)

        # Note: Scanning logic here depends on your keyring backend's ability to list.
        # If listing is unavailable, we iterate known environments to generate slot candidates.
        servers = self._get_server_dict()
        for name, cfg in servers.items():
            # Discovery: Find all slots for this environment
            # (Assuming the manager keeps a registry or the backend supports prefix listing)
            potential_slots = self._list_slots_for_env(name) 
            
            for slot in potential_slots:
                data = self._get_token_meta(slot)
                if data:
                    stored_token = data.get('AccessToken') or data.get('access_token')
                    if stored_token == token_to_match:
                        source_map = {
                            "nickname": name,
                            "user_id": data.get("user_id"), # Found in encrypted blob
                            "url": cfg.get("url"),
                            "company": data.get("current_company"),
                            "last_used": data.get("_last_used"),
                            "meta": data
                        }
                        return tuple(source_map.get(f) for f in fields)
        
        return tuple(None for _ in fields)

    def touch_session(self, slot: str, token_meta: Dict, nickname: str = ""):
        """
        Exclusive bottleneck for session state.
        - Accepts complete token metadata (created by _fetch_token_kinetic or context switch).
        - Registers user in sessions list (zero-trust).
        - Persists to keyring.
        - Sets the Global Last-Run Pointer for Quick Connect.
        
        Args:
            slot: Unique token slot identifier (Nickname-UserID-Hash)
            token_meta: Complete token metadata dict with AccessToken, user_id, etc.
            nickname: Environment nickname for user registration
        """
        now = time.time()
        
        # 1. Validation - token_meta must contain the AccessToken
        if not token_meta.get('AccessToken') and not token_meta.get('access_token'):
            return # Cannot touch a session without a token payload

        # 2. Update timestamp
        token_meta['_last_used'] = now
        
        # 3. Persist to Keyring (Obscured Slot)
        keyring.set_password(slot, "current_token", json.dumps(token_meta))
        
        # 4. Register user in sessions list (CENTRALIZED LOGIC)
        if nickname:
            servers = self._get_server_dict()
            name, cfg = self._find_env(servers, nickname)
            if cfg:
                user_id = token_meta.get("user_id", "")
                if user_id:
                    sessions = cfg.get("sessions", [])
                    if user_id.lower() not in [s.lower() for s in sessions]:
                        sessions.append(user_id)
                        cfg["sessions"] = sessions
                        keyring.set_password(SERVICE_SERVERS, "config", json.dumps(servers))
        
        # 5. Promote to Global Last-Run Pointer
        # This is what prompt_for_env looks for first.
        keyring.set_password("KineticSDK", "LAST_GLOBAL_SESSION", slot)

    def touch_from_headers(self, headers: dict):
        """Extracts context from wire headers and updates the session's current company."""
        auth = headers.get('Authorization', '')
        token = auth.replace("Bearer ", "").strip()
        co_id = headers.get('X-Epicor-Company')
        
        if token:
            # Find who this token belongs to
            nickname, user_id = self.get_session_by_bearer(token, ("nickname", "user_id"))
            if nickname and user_id:
                slot = self._get_token_key(nickname, user_id, self._get_api_key(nickname))
                # Retrieve existing metadata and update company
                meta = self._get_token_meta(slot)
                if meta and co_id:
                    meta['current_company'] = co_id
                    self.touch_session(slot, meta, nickname)

    def sync_companies(self, env_name: str, passive: bool = False):
        """Sync authorized companies from server to local config for one environment."""
        env_name = str(env_name or "").strip()
        if not env_name:
            print("❌ Sync Failure: env name is required")
            return tuple()

        servers = self._get_server_dict()
        name, master_cfg = self._find_env(servers, env_name)
        if not master_cfg:
            print(f"❌ Sync Failure: unknown environment '{env_name}'")
            return tuple()

        local_list = sorted([c.strip() for c in str(master_cfg.get('companies', '')).split(',') if c.strip()])

        url, token, api_key, current_co, user_id, nickname = self.get_active_config(
            name,
            fields=("url", "token", "api_key", "company", "user_id", "nickname"),
        )

        if not token or not url:
            print(f"❌ Sync Failure: No valid session or URL for {name}")
            return tuple(local_list)

        env_match, actual_user = self.get_session_by_bearer(token, ("nickname", "user_id"))
        if env_match and env_match.lower() != str(nickname or name).lower():
            print(f"❌ Sync Failure: Token belongs to {env_match}, but targeting {nickname or name}! Aborting.")
            return tuple(local_list)
        if actual_user and user_id and str(actual_user).lower() != str(user_id).lower():
            print(f"❌ Sync Failure: Token user is {actual_user}, but targeting {user_id}! Aborting.")
            return tuple(local_list)

        base_url = str(url).rstrip('/')
        user_id_wc = str(user_id or "").replace("'", "''")
        bo_path = "Erp.BO.UserFileSvc/GetRows"

        candidate_urls = []
        base_lower = base_url.lower()
        if "/api/v2/" in base_lower:
            candidate_urls.append(f"{base_url}/{bo_path}")
            api_v2_root = base_url[:base_lower.index("/api/v2/") + len("/api/v2")]
            candidate_urls.append(f"{api_v2_root}/{bo_path}")
        else:
            candidate_urls.append(f"{base_url}/api/v2/odata/{current_co}/{bo_path}")
            candidate_urls.append(f"{base_url}/api/v2/{bo_path}")

        ordered_urls = []
        seen_urls = set()
        for candidate_url in candidate_urls:
            normalized_url = candidate_url.rstrip('/')
            if normalized_url and normalized_url not in seen_urls:
                ordered_urls.append(normalized_url)
                seen_urls.add(normalized_url)

        params = {
            "whereClauseUserFile": f"DcdUserID = '{user_id_wc}'",
            "whereClauseUserComp": f"DcdUserID = '{user_id_wc}'",
            "whereClauseUserCompExt": "",
            "pageSize": 50,
            "absolutePage": 1,
        }

        headers = self.get_auth_headers({
            "token": token,
            "api_key": api_key,
            "company": current_co,
        })
        headers["X-Company"] = str(current_co or "")

        try:
            response = None
            last_error = None
            did_retry_refresh = False
            slot = self._get_token_key(name, user_id, api_key)

            for api_url in ordered_urls:
                response = requests.get(api_url, headers=headers, params=params, timeout=20)

                if response.status_code == 401 and "invalid api key" in (response.text or "").lower():
                    self.log_wire("GET", api_url, headers, resp=response)
                    print(
                        "❌ Sync Error: Server rejected the API key for this environment/company. "
                        "Update the environment API key or its company scope, then retry."
                    )
                    return tuple(local_list)

                if response.status_code == 401 and not did_retry_refresh:
                    print(f"⚠️ Received 401 from {nickname or name}; attempting one token refresh and retry...")
                    renew_ctx = {
                        "url": base_url,
                        "api_key": api_key,
                        "user_id": user_id,
                        "token_slot": slot,
                        "nickname": name,
                        "company": current_co,
                    }

                    existing_token = headers.get("Authorization", "").replace("Bearer ", "").strip()
                    renewed = self._renew_token_from_existing(renew_ctx, existing_token) if existing_token else None
                    if not renewed and not passive:
                        renewed = self._fetch_token_kinetic(renew_ctx)

                    if renewed:
                        refreshed = self._get_token_meta(slot) or {}
                        fresh_token = str(refreshed.get("AccessToken") or refreshed.get("access_token") or "").strip()
                        if fresh_token:
                            headers = self.get_auth_headers({
                                "token": fresh_token,
                                "api_key": api_key,
                                "company": current_co,
                            })
                            headers["X-Company"] = str(current_co or "")
                            response = requests.get(api_url, headers=headers, params=params, timeout=20)
                    did_retry_refresh = True

                self.log_wire("GET", api_url, headers, resp=response)

                if response.ok:
                    break

                last_error = f"{response.status_code} for {api_url}"

            if not response or not response.ok:
                if response is not None:
                    response.raise_for_status()
                raise RuntimeError(last_error or "no response from sync probe")

            data = response.json() or {}
            server_set = set()
            return_obj = data.get("returnObj") if isinstance(data, dict) else {}
            comps = return_obj.get("UserComp", []) if isinstance(return_obj, dict) else []
            if isinstance(comps, list):
                for item in comps:
                    if not isinstance(item, dict):
                        continue
                    company = str(item.get("Company") or item.get("CompanyID") or "").strip()
                    if company:
                        server_set.add(company)

            server_list = sorted(server_set)
            if not server_list:
                print(f"⚠️ Sync: server returned no companies for {nickname or name}; keeping local list.")
                return tuple(local_list)

            if server_list == local_list:
                print(f"✅ Sync: {nickname or name} company list is up to date.")
                return tuple(local_list)

            print(f"⚠️ Mismatch detected for {nickname or name}!")
            print(f"   Server: {', '.join(server_list)}")
            print(f"   Local:  {', '.join(local_list)}")

            if passive:
                return tuple(local_list)

            choice = input("\nUpdate company list? [Y]es (Overwrite) / [M]erge / [N]o: ").strip().lower()
            if choice in {"", "y", "yes"}:
                final_list = server_list
            elif choice in {"m", "merge"}:
                final_list = sorted(list(set(local_list + server_list)))
            else:
                return tuple(local_list)

            master_cfg["companies"] = ",".join(final_list)
            keyring.set_password(SERVICE_SERVERS, "config", json.dumps(servers))
            print(f"✅ Updated list saved for {nickname or name}.")
            return tuple(final_list)

        except Exception as e:
            print(f"❌ Sync Error: {e}")
            return tuple(local_list)
        
    def _print_env_var(self, key: str, value: str):
        """Print a shell command that sets an environment variable.

        Values are masked when printed to an interactive terminal to avoid
        accidental disclosure. The actual (unmasked) value is not changed or
        re-stored by this helper; callers should use the print output as a
        convenience for users to capture into their shell session if desired.
        """
        display = value or ""
        if self.is_interactive and value:
            # Mask API keys and tokens for display purposes only.
            if "API_KEY" in key and len(value) > 4:
                display = f"{value[:4]}****************"
            elif "TOKEN" in key:
                display = "****************"

        if self.os_type == "windows":
            print(f"SET {key}={display}")
        else:
            print(f"export {key}=\"{display}\"")

    def use(self, context: any = None, user: str = ""):
        """CLI ACTION: Selects environment and prints SET/export lines."""
        user = (user or "").strip()
        if user:
            sys.stderr.write(
                "⚠️  --user was passed on the command line; it can be captured in "
                "shell history and is visible to other local users via process "
                "listings (ps/Task Manager). Prefer the KIN_USER environment "
                "variable instead.\n"
            )

        # 1. If no context (env) provided via CLI, trigger the interactive prompt
        if not context:
            env_name, user_id, co_id = self.prompt_for_env()
            context = (env_name, user_id, co_id)
        elif user:
            context = (context, user)

        # 2. Resolve the full config using the standardized "Triple" fields
        # This fixes the TypeError by providing the 'fields' argument
        config_data = self.get_active_config(
            context,
            fields=("url", "token", "api_key", "company", "nickname", "user_id")
        )

        if not config_data[0]:
            # Raise rather than return: callers capture this output as shell
            # exports, so a silent exit 0 would look like a successful switch.
            env_label = context[0] if isinstance(context, (tuple, list)) else context
            servers = self._get_server_dict()
            if not servers:
                raise SessionResolutionError(
                    f"Could not resolve session for '{env_label}': no environments "
                    "are stored yet. Run 'store' first to add one."
                )
            if self._find_env(servers, env_label or "")[1] is None:
                raise SessionResolutionError(
                    f"Could not resolve session for '{env_label}': no stored "
                    f"environment by that name. Stored: {', '.join(sorted(servers))}."
                )
            raise SessionResolutionError(f"Could not resolve session for '{env_label}'.")

        # Map results for display
        _url, _token, api_key, company, nickname, resolved_user_id = config_data

        # 3. Print environment variables for the shell. Emit the same
        # selectors KineticBaseClient.__init__ reads (KINETIC_SESSION_*) so
        # that capturing this output actually activates the session for
        # other tools, not just KIN_COMPANY.
        print(f"\n# Active Session: {nickname} (Co: {company})")
        self._print_env_var("KIN_COMPANY", company)
        self._print_env_var("KINETIC_SESSION_ENV", nickname)
        self._print_env_var("KINETIC_SESSION_USER", resolved_user_id)
        self._print_env_var("KINETIC_SESSION_CO", company)

        # Resolving from a cached token (no fresh fetch) never calls
        # touch_session, so LAST_GLOBAL_SESSION can still point at a
        # different, previously-active session. Update it explicitly so a
        # bare `use` (no args) elsewhere picks up what was just activated.
        if resolved_user_id and api_key:
            try:
                slot = self._get_token_key(nickname, resolved_user_id, api_key)
                keyring.set_password("KineticSDK", "LAST_GLOBAL_SESSION", slot)
            except Exception:
                pass

    def _select_session_for_env(
        self, selected_env: str, env_cfg: Dict, preferred_company: str = ""
    ) -> Tuple[str, str]:
        """Interactively select an existing session or enter a new Epicor
        User ID for an already-known environment, then pick a company if
        more than one is configured.

        This never re-selects the environment itself -- it's shared by
        prompt_for_env() (which picks the environment first) and
        get_active_config()'s explicit-environment path (which already
        knows the environment and must stay scoped to it).

        If preferred_company is already set (e.g. an explicit --company
        was supplied by the caller), company selection is skipped entirely
        so an interactive prompt here never overwrites it.
        """
        sessions = env_cfg.get("sessions", []) or []
        selected_user = None
        if sessions:
            print(f"\n👤 Sessions for {selected_env}:")
            for i, user in enumerate(sessions, 1):
                print(f"   {i}) {user}")
            u_choice = input(f"Select Session (0-{len(sessions)}) [1]: ").strip() or "1"
            if u_choice != "0":
                selected_user = sessions[int(u_choice)-1]

        # If no session was selected, insist on a non-empty user id input.
        if not selected_user:
            while True:
                entered = input("Enter Epicor User ID: ").strip()
                if entered:
                    selected_user = entered
                    break
                print("User ID cannot be empty. Please enter your Epicor User ID.")

        # SELECT COMPANY (Supports Direct ID or Numeric Selection)
        if preferred_company:
            selected_co = preferred_company
            self.set_current_company(selected_env, selected_user, selected_co)
            return selected_user, selected_co

        raw_cos = env_cfg.get('company') or env_cfg.get('companies') or ""
        available_cos = [c.strip() for c in str(raw_cos).split(',') if c.strip()]

        if len(available_cos) > 1:
            print(f"\n🏢 Select Company for {selected_env}:")
            for i, co in enumerate(available_cos, 1):
                print(f"  {i}) {co}")

            choice = input(f"Selection (1-{len(available_cos)}) or enter ID [1]: ").strip() or "1"

            # 1. Check if the user typed the ID directly (e.g., "ACME")
            if choice.upper() in [c.upper() for c in available_cos]:
                selected_co = next(c for c in available_cos if c.upper() == choice.upper())
            else:
                try:
                    # 2. Otherwise, treat it as a numeric index
                    idx = int(choice) - 1
                    selected_co = available_cos[idx]
                except (ValueError, IndexError):
                    # 3. Fallback to first if input is garbage
                    print(f"⚠️ Invalid selection. Defaulting to {available_cos[0]}")
                    selected_co = available_cos[0]
        else:
            selected_co = available_cos[0] if available_cos else ""

        # Lock choice into metadata
        self.set_current_company(selected_env, selected_user, selected_co)
        return selected_user, selected_co

    def prompt_for_env(self, passive: bool = False, prompt_reuse: bool = False) -> Tuple[str, str, str]:
        # 1. GLOBAL QUICK-CONNECT: Check the last session touched by ANY tool
        last_slot = keyring.get_password("KineticSDK", "LAST_GLOBAL_SESSION")
        
        if last_slot and not passive:
            # Be defensive: only auto-reuse the pointer when it actually
            # belongs to a currently configured environment and user.
            meta = self._get_token_meta(last_slot)
            if meta and meta.get("_is_valid"):
                env_guess = meta.get("env_name") or str(last_slot).split('-')[0]
                co = meta.get("current_company", "Unknown")

                # Confirm the environment still exists and the api_key matches
                servers = self._get_server_dict()
                name, cfg = self._find_env(servers, env_guess)
                display_id = last_slot[-4:].upper()

                # Validate that the slot actually matches the current env/user/api_key
                user_id = meta.get("user_id")
                slot_valid = False
                if name and cfg and user_id:
                    try:
                        expected = self._get_token_key(name, user_id, cfg.get('api_key', ''))
                        if expected == last_slot:
                            # Ensure the user is recorded in sessions for this env
                            sessions = cfg.get('sessions', []) or []
                            if any(user_id.lower() == s.lower() for s in sessions):
                                slot_valid = True
                    except Exception:
                        slot_valid = False

                # Require that the token metadata already contains a valid
                # current_company before offering to reuse. If the company is
                # missing, prompt the user to select an environment+company
                # instead of silently reusing an incomplete session.
                if slot_valid and meta.get('current_company'):
                    print(f"\n✨ Last active: ID_{display_id} @ {name} (Co: {co})")
                    # Default behavior is to reuse immediately. To restore
                    # interactive confirmation, either pass prompt_reuse=True
                    # or set KIN_PROMPT_REUSE=1.
                    should_prompt_reuse = prompt_reuse or os.getenv("KIN_PROMPT_REUSE", "").strip().lower() in {
                        "1", "true", "yes", "y", "on"
                    }
                    if should_prompt_reuse:
                        if input("Reuse this session? [Y/n]: ").strip().lower() in ('', 'y', 'yes'):
                            return name, user_id, co
                    else:
                        print("Auto-reusing last active session.")
                        return name, user_id, co

        # 3. SELECT ENVIRONMENT
        configs = self._get_server_dict()
        options = list(configs.keys())
        print("\n--- Select Kinetic Environment ---")
        for i, n in enumerate(options, 1):
            print(f"{i}) 🟢 {n:<15} ({configs[n]['url']})")
        
        choice = input(f"\nSelection (1-{len(options)}) [1]: ").strip() or "1"
        selected_env = options[int(choice)-1]
        env_cfg = configs[selected_env]

        if passive: return selected_env, "", ""

        # 4 & 5. SELECT SESSION AND COMPANY (shared with get_active_config's
        # explicit-environment path -- see _select_session_for_env)
        selected_user, selected_co = self._select_session_for_env(selected_env, env_cfg)
        return selected_env, selected_user, selected_co

    def logout(self, env_name: str):
        """Clears all session hashes for a parent environment."""
        servers = self._get_server_dict()
        name, cfg = self._find_env(servers, env_name)
        if not cfg: return

        sessions = cfg.get("sessions", [])
        for user in sessions:
            slot = self._get_token_key(name, user, cfg['api_key'])
            try:
                keyring.delete_password(slot, "current_token")
                print(f"🗑️ Cleared session for {user}")
            except: pass
        
        # Clear the session list in config too
        cfg["sessions"] = []
        keyring.set_password(SERVICE_SERVERS, "config", json.dumps(servers))
        print(f"✅ All sessions for {name} have been purged.")

    def inspect(self, env_name: str):
        """Lists all active user sessions and the redacted server configuration."""
        servers = self._get_server_dict()
        name, cfg = self._find_env(servers, env_name)
        if not cfg:
            print(f"No environment named '{env_name}' found.")
            return

        print(f"\n--- Environment Audit: {name} ---")
        
        # --- Redacted Config Section ---
        api_key = cfg.get('api_key', '')
        redacted_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "****"
        
        print(f"URL:       {cfg.get('url')}")
        print(f"API Key:   {redacted_key}")
        print(f"Companies: {cfg.get('companies', 'None')}") 
        
        # --- Session Section ---
        sessions = cfg.get("sessions", [])
        if not sessions:
            print(f"\nNo recorded sessions for {name}. Use 'auth login' to begin.")
            return

        print(f"\n{'User':<15} | {'Status':<10} | {'Remaining':<12} | {'Current Co'}")
        print("-" * 65)
        for user in sessions:
            slot = self._get_token_key(name, user, cfg['api_key'])
            data = self._get_token_meta(slot)
            
            if data:
                rem = data.get("_remaining", 0)
                status = "✅ ACTIVE" if data.get("_is_valid") else "❌ EXPIRED"
                # Pull current company from token metadata
                current_co = data.get("current_company", "---")
                
                print(f"{data.get('user_id', user):<15} | {status:<10} | "
                      f"{str(timedelta(seconds=rem)):<12} | {current_co}")
            else:
                print(f"{user:<15} | ⚠️ NO TOKEN | {'-':<12} | ---")

    def set_current_company(self, nickname: str, user_id: str, company_id: str):
        """Updates the token metadata in the keyring to track the 'active' company."""
        servers = self._get_server_dict()
        name, cfg = self._find_env(servers, nickname)
        if not cfg: return

        slot = self._get_token_key(name, user_id, cfg['api_key'])
        data = self._get_token_meta(slot)
        
        if data:
            data['current_company'] = company_id
            # Re-save the updated JSON blob to the keyring
            keyring.set_password(slot, "current_token", json.dumps(data))

    def get_all_configs(self) -> Dict[str, Dict]:
        """Module API: Returns a dictionary of all stored configs with their current token status."""
        servers = self._get_server_dict()
        results = {}
        for name, cfg in servers.items():
            # We track tokens per user, so we check the 'sessions' list for the first active one.
            sessions = cfg.get("sessions", [])
            active_meta = None
            
            if sessions:
                # Look for the most recently used or first valid session
                for user in sessions:
                    slot = self._get_token_key(name, user, cfg['api_key'])
                    meta = self._get_token_meta(slot)
                    if meta and meta.get("_is_valid"):
                        active_meta = meta
                        break
            
            results[name] = {
                "url": cfg.get('url'),
                "companies": cfg.get('companies'),
                "is_active": active_meta["_is_valid"] if active_meta else False,
                "remaining_sec": active_meta["_remaining"] if active_meta else 0,
                "status_label": "active" if active_meta and active_meta["_is_valid"] else ("expired" if active_meta else "no token")
            }
        return results

    def list(self):
        """CLI ACTION: List saved nicknames with URL, companies, and token status."""
        configs = self.get_all_configs()
        print(f"\n{'Nickname':<12} | {'URL':<40} | {'Companies':<20} | {'Status'}")
        print("-" * 105)
        
        for name, info in configs.items():
            co_list = str(info.get("companies", "None"))
            # Your original truncation logic
            display_co = (co_list[:17] + "..") if len(co_list) > 17 else co_list

            # Redact URL for display
            url_str = info.get('url', '')
            display_url = url_str
            try:
                parsed_url = urlparse(url_str)
                if parsed_url.hostname:
                    redacted_host = self.redact_value(parsed_url.hostname)
                    display_url = parsed_url._replace(netloc=redacted_host).geturl()
            except Exception:
                # Fallback to redacting the whole string if parsing fails
                display_url = self.redact_value(url_str)

            status = ""
            if info["is_active"]:
                rem = info["remaining_sec"]
                # Displaying minutes left as per your original design
                status = f"✅ {rem//60}m left"
            elif info["status_label"] == "expired":
                status = "❌ Expired"
            else:
                status = "Empty"
            
            print(f"{name:<12} | {display_url:<40} | {display_co:<20} | {status}")

    def store(self):
        """Stores environment and initializes an empty session list."""
        name = input("Nickname (e.g., DEV): ").strip()
        url = input("Base URL: ").strip()
        co = input("Company ID: ").strip()
        key = getpass.getpass("API Key (Scoped): ").strip()
        
        if input(f"Save '{name}'? (y/n): ").lower() == 'y':
            servers = self._get_server_dict()
            # Initialize with an empty sessions list if new
            servers[name] = {
                "url": url, 
                "companies": co, 
                "api_key": key, 
                "sessions": servers.get(name, {}).get("sessions", []) 
            }
            keyring.set_password(SERVICE_SERVERS, "config", json.dumps(servers))
            print(f"✅ Stored {name}")


    def _fetch_token_kinetic(self, ctx: Dict) -> Optional[str]:
        """Interactive credential prompt that saves the UserID into the metadata.

        Accepts a single context dict with keys: url, api_key, user_id, token_slot,
        nickname, company. This consolidates the input and makes it easier to
        pass through the selected company from upstream callers.
        """
        url = ctx.get('url', '')
        api_key = ctx.get('api_key', '')
        user_id = ctx.get('user_id', '')
        token_slot = ctx.get('token_slot', '')
        nickname = ctx.get('nickname', '')
        company = ctx.get('company', '')

        # If the caller did not provide a user_id (empty), prompt now.
        if not user_id:
            while True:
                user_id = input("Epicor User ID: ").strip()
                if user_id:
                    break
                print("User ID cannot be empty. Please enter your Epicor User ID.")

        password = getpass.getpass(f"Password for {user_id}: ").strip()
        auth_url = f"{url.rstrip('/')}/TokenResource.svc/"
        basic_auth = base64.b64encode(f"{user_id}:{password}".encode("utf-8")).decode("ascii")
        primary_headers = {
            'Authorization': f'Basic {basic_auth}',
            'X-API-Key': api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        legacy_headers = {
            'userName': user_id,
            'password': password,
            'x-api-key': api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        try:
            resp = requests.post(auth_url, headers=primary_headers, data='', timeout=15)
            primary_status = resp.status_code
            primary_body = (resp.text or "")[:220]

            if resp.status_code in (400, 401, 403):
                legacy_resp = requests.post(auth_url, headers=legacy_headers, data='', timeout=15)
                legacy_status = legacy_resp.status_code
                legacy_body = (legacy_resp.text or "")[:220]

                if not legacy_resp.ok:
                    detail_text = " ".join([primary_body, legacy_body]).lower()
                    if "invalid api key" in detail_text:
                        raise RuntimeError(
                            "Token endpoint rejected the API key. Update the environment API key "
                            "or verify its company scope for this tenant."
                        )
                    if primary_status == 401 or legacy_status == 401:
                        raise RuntimeError(
                            "Token endpoint rejected credentials (401). Verify Epicor user ID/password "
                            "and ensure the account is allowed for this environment."
                        )
                resp = legacy_resp
            resp.raise_for_status()
            res = resp.json()

            # --- ADD REQUIRED METADATA ---
            res["_local_timestamp"] = time.time()
            res["_last_used"] = time.time()
            res["user_id"] = user_id.lower()  # Store username so 'inspect' can show it
            res["env_name"] = nickname # Track source env

            # --- DETERMINE & SAVE current_company (so token is usable immediately) ---
            # If caller provided a company (e.g., from prompt_for_env), use it.
            try:
                if company:
                    res['current_company'] = company
                else:
                    servers = self._get_server_dict()
                    name, cfg = self._find_env(servers, nickname)
                    raw_cos = cfg.get('companies') or cfg.get('company') if cfg else ''
                    available_cos = [c.strip() for c in str(raw_cos).split(',') if c.strip()]

                    if available_cos:
                        if len(available_cos) == 1:
                            selected_co = available_cos[0]
                        else:
                            # Prompt user to choose a company for this session
                            print(f"\n🏢 Select Company for {nickname} (for user {user_id}):")
                            for i, co in enumerate(available_cos, 1):
                                print(f"  {i}) {co}")
                            choice = input(f"Selection (1-{len(available_cos)}) or enter ID [1]: ").strip() or "1"
                            if choice.upper() in [c.upper() for c in available_cos]:
                                selected_co = next(c for c in available_cos if c.upper() == choice.upper())
                            else:
                                try:
                                    idx = int(choice) - 1
                                    selected_co = available_cos[idx]
                                except Exception:
                                    selected_co = available_cos[0]
                        if selected_co:
                            res['current_company'] = selected_co
            except Exception:
                # Best-effort only; if something goes wrong we'll leave current_company absent
                pass

            selected_company = str(res.get("current_company") or company or "").strip()
            probe_ok, probe_reason = self._validate_bearer_access(
                url=url,
                api_key=api_key,
                company=selected_company,
                token=str(res.get('AccessToken') or res.get('access_token') or '').strip(),
                user_id=user_id,
            )
            if not probe_ok:
                sys.stderr.write(
                    f"\n❌ Auth Failed for {user_id}: token minted but BO probe failed ({probe_reason}).\n"
                )
                return None

            # --- SAVE & REGISTER VIA CENTRALIZED BOTTLENECK ---
            # touch_session receives complete token data and handles all registration
            self.touch_session(token_slot, res, nickname)

            return res.get('AccessToken') or res.get('access_token')
        except Exception as e:
            sys.stderr.write(f"\n❌ Auth Failed for {user_id}: {e}\n")
            return None

    def migrate(self):
        """Migrate legacy server entries into the current normalized shape.

        This is a non-destructive rewrite of the stored servers map that
        attempts to harmonize older keys (e.g., `company`) into the current
        `companies` field and ensures each entry contains a `display_name`.
        """
        servers = self._get_server_dict()
        new_servers = {}
        for k, v in servers.items():
            if not isinstance(v, dict):
                continue
            entry = dict(v)
            entry["companies"] = entry.get("companies") or entry.pop("company", None) or "ACME"
            entry.pop("company", None)
            entry.setdefault("display_name", k)
            new_servers[k] = entry
        keyring.set_password(SERVICE_SERVERS, "config", json.dumps(new_servers))
        print("✅ Migration complete.")

    def set_api_key(self, env_name: str, value: str = ""):
        """Replace only the API key for an existing environment."""
        env_name = str(env_name or "").strip()
        if not env_name:
            print("❌ Environment name is required.")
            return

        servers = self._get_server_dict()
        name, cfg = self._find_env(servers, env_name)
        if not cfg:
            print(f"❌ No environment named '{env_name}' found.")
            return

        new_key = str(value or "").strip()
        if not new_key:
            new_key = getpass.getpass(f"New API key for {name}: ").strip()
        if not new_key:
            print("❌ API key cannot be empty.")
            return

        old_key = str(cfg.get("api_key") or "").strip()
        if old_key == new_key:
            print(f"✅ API key for {name} is unchanged.")
            return

        deleted_slots = 0
        sessions = cfg.get("sessions", []) or []
        for user in sessions:
            user_id = str(user or "").strip()
            if not user_id:
                continue
            try:
                old_slot = self._get_token_key(name, user_id, old_key)
                keyring.delete_password(old_slot, "current_token")
                deleted_slots += 1
            except Exception:
                pass

        cfg["api_key"] = new_key
        servers[name] = cfg
        keyring.set_password(SERVICE_SERVERS, "config", json.dumps(servers))

        try:
            last_slot = keyring.get_password("KineticSDK", "LAST_GLOBAL_SESSION") or ""
            if last_slot and any(last_slot == self._get_token_key(name, str(user), old_key) for user in sessions if str(user).strip()):
                keyring.delete_password("KineticSDK", "LAST_GLOBAL_SESSION")
        except Exception:
            pass

        print(f"✅ API key updated for {name}.")
        if deleted_slots:
            print(f"Removed {deleted_slots} cached token slot(s) for {name}; renew to create a fresh token.")

    def _renew_token_from_existing(self, ctx: Dict, existing_token: str) -> Optional[str]:
        """Attempt to obtain a fresh JWT using an existing bearer token."""
        url = str(ctx.get("url") or "").strip().rstrip("/")
        api_key = str(ctx.get("api_key") or "").strip()
        user_id = str(ctx.get("user_id") or "").strip().lower()
        token_slot = str(ctx.get("token_slot") or "").strip()
        nickname = str(ctx.get("nickname") or "").strip()
        company = str(ctx.get("company") or "").strip()

        if not url or not api_key or not token_slot or not existing_token:
            return None

        candidates = [
            ("POST", f"{url}/TokenResource.svc/", ""),
            ("POST", f"{url}/TokenResource.svc/RefreshToken", ""),
            ("POST", f"{url}/TokenResource.svc/refresh", ""),
            ("GET", f"{url}/TokenResource.svc/", None),
        ]

        base_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "Authorization": f"Bearer {existing_token}",
        }
        if company:
            base_headers["X-Epicor-Company"] = company

        for method, endpoint, payload in candidates:
            try:
                if method == "POST":
                    resp = requests.post(endpoint, headers=base_headers, data=payload, timeout=15)
                else:
                    resp = requests.get(endpoint, headers=base_headers, timeout=15)
                resp.raise_for_status()
                data = resp.json() if resp.content else {}
                if not isinstance(data, dict):
                    continue

                token = str(data.get("AccessToken") or data.get("access_token") or "").strip()
                if not token:
                    continue

                probe_ok, _ = self._validate_bearer_access(
                    url=url,
                    api_key=api_key,
                    company=company,
                    token=token,
                    user_id=user_id,
                )
                if not probe_ok:
                    continue

                data["_local_timestamp"] = time.time()
                data["_last_used"] = time.time()
                data["user_id"] = user_id
                data["env_name"] = nickname
                if company:
                    data["current_company"] = company

                self.touch_session(token_slot, data, nickname)
                return token
            except Exception:
                continue

        return None

    def _validate_bearer_access(
        self,
        url: str,
        api_key: str,
        company: str,
        token: str,
        user_id: str,
    ) -> Tuple[bool, str]:
        """Validate bearer usability against a real BO endpoint."""
        base_url = str(url or "").strip().rstrip("/")
        api_key = str(api_key or "").strip()
        company = str(company or "").strip()
        token = str(token or "").strip()
        user_id = str(user_id or "").strip()
        if not base_url or not api_key or not company or not token:
            return False, "missing probe inputs"

        user_id_wc = user_id.replace("'", "''")
        where = f"DcdUserID = '{user_id_wc}'" if user_id else ""
        params = {
            "whereClauseUserFile": where,
            "whereClauseUserComp": where,
            "whereClauseUserCompExt": "",
            "pageSize": 1,
            "absolutePage": 1,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "X-Epicor-Company": company,
            "X-Company": company,
        }

        bo_path = "Erp.BO.UserFileSvc/GetRows"
        candidates = []
        base_lower = base_url.lower()
        if "/api/v2/" in base_lower:
            candidates.append(f"{base_url}/{bo_path}")
            api_v2_root = base_url[:base_lower.index("/api/v2/") + len("/api/v2")]
            candidates.append(f"{api_v2_root}/{bo_path}")
        else:
            candidates.append(f"{base_url}/api/v2/odata/{company}/{bo_path}")
            candidates.append(f"{base_url}/api/v2/{bo_path}")

        seen = set()
        ordered = []
        for item in candidates:
            normalized = item.rstrip("/")
            if normalized and normalized not in seen:
                ordered.append(normalized)
                seen.add(normalized)

        last_status = "no-response"
        for endpoint in ordered:
            try:
                response = requests.get(endpoint, headers=headers, params=params, timeout=15)
                if response.ok:
                    return True, f"{response.status_code} {endpoint}"
                last_status = str(response.status_code)
                body = (response.text or "")[:200].lower()
                if "invalid api key" in body:
                    return False, "api key rejected"
            except Exception as exc:
                last_status = str(exc)

        return False, f"probe failed: {last_status}"

    def renew(
        self,
        env_name: str = "",
        user_id: str = "",
        company_id: str = "",
        from_token: bool = False,
        allow_password_fallback: bool = False,
    ):
        """Force a fresh JWT fetch from TokenResource and persist it to keyring."""
        env_name = str(env_name or "").strip()
        user_id = str(user_id or "").strip()
        company_id = str(company_id or "").strip()

        servers = self._get_server_dict()

        if not env_name or not user_id:
            selected_env, selected_user, selected_co = self.prompt_for_env()
            env_name = env_name or selected_env
            user_id = user_id or selected_user
            company_id = company_id or selected_co

        name, cfg = self._find_env(servers, env_name)
        if not cfg:
            print(f"❌ No environment named '{env_name}' found.")
            return

        if not user_id:
            sessions = cfg.get("sessions", []) or []
            if sessions:
                user_id = str(sessions[0]).strip()

        if not user_id:
            while True:
                entered = input("Epicor User ID: ").strip()
                if entered:
                    user_id = entered
                    break
                print("User ID cannot be empty. Please enter your Epicor User ID.")

        if not company_id:
            try:
                slot_guess = self._get_token_key(name, user_id, cfg.get("api_key", ""))
                meta = self._get_token_meta(slot_guess) or {}
                company_id = str(meta.get("current_company") or "").strip()
            except Exception:
                company_id = ""

        if not company_id:
            companies = [c.strip() for c in str(cfg.get("companies", "")).split(",") if c.strip()]
            company_id = companies[0] if companies else ""

        api_key = cfg.get("api_key", "")
        slot = self._get_token_key(name, user_id, api_key)
        ctx = {
            "url": cfg.get("url", ""),
            "api_key": api_key,
            "user_id": user_id,
            "token_slot": slot,
            "nickname": name,
            "company": company_id,
        }

        if from_token:
            current_meta = self._get_token_meta(slot) or {}
            current_token = str(current_meta.get("AccessToken") or current_meta.get("access_token") or "").strip()
            renewed = self._renew_token_from_existing(ctx, current_token)
            if renewed:
                print(f"\n✅ Token renewed from existing bearer for {name}/{user_id} (Co: {company_id})")
                self.use((name, user_id, company_id))
                return

            print("❌ Token-based renewal failed for this environment.")
            if not allow_password_fallback:
                print("Tip: rerun with --allow-password-fallback to prompt for password-based token re-issue.")
                return

        token = self._fetch_token_kinetic(ctx)
        if not token:
            print("❌ Token renewal failed.")
            return

        print(f"\n✅ Token renewed for {name}/{user_id} (Co: {company_id})")
        self.use((name, user_id, company_id))

    def delete(self, env_name: str):
        """Delete a saved server entry by nickname (case-insensitive).

        The operation updates the stored servers map in the keyring.
        """
        servers = self._get_server_dict()
        name, _ = self._find_env(servers, env_name)
        if name:
            del servers[name]
            keyring.set_password(SERVICE_SERVERS, "config", json.dumps(servers))
            print(f"Deleted '{name}'.")

    def rename(self, old_name: str, new_name: str, dry_run: bool = False) -> Dict[str, Any]:
        """Rename a saved environment, keeping its stored credentials.

        Moves the config entry to the new nickname, copies every cached token
        slot to its new slot name byte-for-byte (encrypted slots stay
        encrypted, so no passphrase or re-login is needed for them), repoints
        LAST_GLOBAL_SESSION if it referenced a moved slot, and only then
        deletes the old slots -- a failure part-way leaves the old entry intact.
        An encrypted servers map is re-encrypted with the same passphrase.
        """
        new_name = (new_name or "").strip()
        if not new_name or any(ch.isspace() for ch in new_name):
            raise ValueError("New nickname must be non-empty and contain no whitespace.")

        raw_config = keyring.get_password(SERVICE_SERVERS, "config") or ""
        vault_encrypted = crypto.is_encrypted_blob(raw_config) if raw_config else False
        passphrase = None
        if vault_encrypted:
            passphrase = os.environ.get("KINETIC_VAULT_PASSPHRASE")
            if not passphrase and sys.stdout.isatty():
                passphrase = getpass.getpass("Vault passphrase: ")
            if not passphrase:
                raise RuntimeError("Vault is encrypted. Set KINETIC_VAULT_PASSPHRASE or run interactively.")
            servers = crypto.decrypt_json(raw_config, passphrase)
        else:
            servers = self._get_server_dict()

        old_key, cfg = self._find_env(servers, old_name)
        if not cfg:
            raise ValueError(f"No saved environment named '{old_name}'.")
        clash, _ = self._find_env(servers, new_name)
        if clash and clash != old_key:
            raise ValueError(f"An environment named '{clash}' already exists.")
        if old_key == new_name:
            raise ValueError(f"'{old_key}' already has that name.")

        api_key = cfg.get("api_key", "")
        moves = []
        for user in cfg.get("sessions", []):
            old_slot = self._get_token_key(old_key, user, api_key)
            new_slot = self._get_token_key(new_name, user, api_key)
            raw = keyring.get_password(old_slot, "current_token")
            moves.append({"user": user, "old_slot": old_slot, "new_slot": new_slot, "raw": raw})
        last_slot = keyring.get_password("KineticSDK", "LAST_GLOBAL_SESSION") or ""
        repoint = next((m["new_slot"] for m in moves if m["old_slot"] == last_slot), None)

        summary = {
            "old": old_key,
            "new": new_name,
            "sessions_moved": [m["user"] for m in moves if m["raw"]],
            "sessions_without_cached_token": [m["user"] for m in moves if not m["raw"]],
            "pointer_moved": bool(repoint),
            "dry_run": dry_run,
        }
        if dry_run:
            return summary

        # 1. New slots first (old ones still exist if anything below fails).
        for move in moves:
            if move["raw"]:
                keyring.set_password(move["new_slot"], "current_token", move["raw"])

        # 2. Config entry, keeping its position and the vault's encryption.
        renamed = {(new_name if key == old_key else key): value for key, value in servers.items()}
        payload = crypto.encrypt_json(renamed, passphrase) if vault_encrypted else json.dumps(renamed)
        keyring.set_password(SERVICE_SERVERS, "config", payload)

        # 3. Global pointer.
        if repoint:
            keyring.set_password("KineticSDK", "LAST_GLOBAL_SESSION", repoint)

        # 4. Old slots. Credential stores can match names case-insensitively
        # (Windows Credential Manager), so for a case-only rename the "old"
        # slot is the one just written -- never delete it.
        for move in moves:
            if move["raw"] and move["old_slot"].lower() != move["new_slot"].lower():
                try:
                    keyring.delete_password(move["old_slot"], "current_token")
                except Exception:
                    pass
        return summary

    def get_base_config(self, env_name: str) -> Optional[Dict]:
        """
        Retrieves stored environment metadata without attempting to 
        authenticate or fetch a token. Useful for legacy shell exports.
        """
        servers = self._get_server_dict()
        name, cfg = self._find_env(servers, env_name)
        if not cfg:
            return None
        
        return {
            "url": cfg.get('url'),
            "companies": cfg.get('companies'),
            "api_key": cfg.get('api_key'),
            "nickname": name
        }

    def get_active_config(self, context: any, fields: Tuple[str, ...]) -> Tuple:
        """
        Accepts context as:
        - "" / None (no environment named: reuse the last globally-active session)
        - "nickname" (Uses last active user/company for that environment)
        - ("nickname", "user_id") (Uses last active company)
        - {"env": "...", "user": "...", "co": "..."} (Precise)

        When an environment is explicitly named, resolution stays strictly
        scoped to it. It will never silently substitute a different
        environment via the global "last active" pointer -- ambiguous or
        unresolvable sessions for a named environment raise
        SessionResolutionError instead of drifting.
        """
        # --- 1. UNPACK CONTEXT ---
        env_name, user_id, company_id = None, None, None

        if isinstance(context, str):
            env_name = context
        elif isinstance(context, (tuple, list)):
            env_name = context[0]
            if len(context) > 1: user_id = context[1]
            if len(context) > 2: company_id = context[2]
        elif isinstance(context, dict):
            env_name = context.get('env') or context.get('nickname')
            user_id = context.get('user') or context.get('user_id')
            company_id = context.get('co') or context.get('company')

        explicit_env = bool(env_name)

        # Preferred way to disambiguate a session without leaking usernames
        # into shell history: set KIN_USER rather than passing it on a CLI
        # flag. Only used as a fallback -- an explicit user_id already in
        # context (e.g. from a --user flag) wins.
        if not user_id:
            env_user = os.getenv("KIN_USER", "").strip()
            if env_user:
                user_id = env_user

        # --- 2. RESOLVE MISSING PIECES ---
        servers = self._get_server_dict()
        name, cfg = self._find_env(servers, env_name)
        if not cfg: return tuple(None for _ in fields)

        # Fallback for user: Use the session if there's exactly one.
        if not user_id:
            sessions = cfg.get("sessions", []) or []
            if len(sessions) == 1:
                user_id = sessions[0]
            elif explicit_env:
                # An environment was explicitly named -- stay scoped to it
                # instead of falling back to prompt_for_env(), which would
                # silently resolve a DIFFERENT environment via the global
                # last-active pointer.
                if sys.stdin.isatty() and sys.stdout.isatty():
                    # Interactive: prompt for (or create) a session scoped to
                    # this environment only -- never re-picks the environment.
                    # An explicitly-supplied company (e.g. via --company) is
                    # passed through so the prompt never overwrites it.
                    user_id, company_id = self._select_session_for_env(
                        name, cfg, preferred_company=company_id or ""
                    )
                else:
                    # Non-interactive: fail closed and loud. No session
                    # identifiers are included in the message.
                    reason = "multiple sessions recorded" if sessions else "no sessions recorded"
                    raise SessionResolutionError(
                        f"Cannot resolve a session for '{name}': {reason}. "
                        f"Set KIN_USER (preferred) or pass --user to disambiguate."
                    )
            else:
                # No environment was named at all: reuse the last globally-
                # active session. This is the only path allowed to pick a
                # different environment than what was asked for, because
                # nothing specific was asked for.
                env_name, user_id, company_id = self.prompt_for_env()
                name, cfg = self._find_env(servers, env_name)
                if not cfg: return tuple(None for _ in fields)

        if not user_id: return tuple(None for _ in fields)

        # Resolve token & meta
        api_key = cfg['api_key']
        slot = self._get_token_key(name, user_id, api_key)
        meta = self._get_token_meta(slot)

        # (Insert Auth/Fetch Logic here...)
        # CORRECTED LOGIC:
        token = None
        # Only reuse a cached token when it's valid AND the user_id is
        # recorded in the environment's sessions list. This prevents
        # using orphaned keyring blobs that are not associated with
        # the saved server config.
        if meta and meta.get("_is_valid"):
            sessions = cfg.get('sessions', []) or []
            if user_id and any(user_id.lower() == s.lower() for s in sessions):
                token = meta.get('AccessToken') or meta.get('access_token')
            else:
                # If the cached token exists but the user isn't recorded
                # in the sessions list, force an interactive fetch so the
                # session is created through the centralized touch_session
                ctx = {
                    'url': cfg.get('url'),
                    'api_key': api_key,
                    'user_id': user_id,
                    'token_slot': slot,
                    'nickname': name,
                    'company': company_id
                }
                token = self._fetch_token_kinetic(ctx)
        else:
            # Pass the variables resolved earlier in the method
            ctx = {
                'url': cfg.get('url'),
                'api_key': api_key,
                'user_id': user_id,
                'token_slot': slot,
                'nickname': name,
                'company': company_id
            }
            token = self._fetch_token_kinetic(ctx)

        # If we performed an interactive fetch above, refresh the meta
        # so that subsequent logic (company resolution) sees any fields
        # added during touch_session (e.g., current_company)
        try:
            meta = self._get_token_meta(slot) or meta
        except Exception:
            pass

        # --- 3. THE "CLEAN" RESOLUTION ---
        # If no company provided, check meta, then fallback to first valid ID in sync list
        active_co = company_id or (meta.get('current_company') if meta else None)
        if not active_co or ',' in str(active_co):
            active_co = [c.strip() for c in cfg.get('companies', '').split(',') if c.strip()][0]

        # --- 4. MAP & RETURN ---
        source_map = {
            "nickname": name,
            "user_id": user_id,
            "url": cfg.get("url"),
            "token": token,
            "api_key": api_key,
            "company": active_co,
            "meta": meta
        }
        return tuple(source_map.get(f) for f in fields)

    def get_auth_headers(self, config: Dict, plant_id: str = "") -> Dict:
        """
        Generates standard headers for Epicor V2 OData calls.
        'self' must be the first argument.
        """
        if not config:
            raise ValueError("No configuration provided for header generation.")

        # We pull the values out of the 'config' dictionary passed in
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-Epicor-Company': config.get('company'),
            'X-API-Key': str(config.get('api_key', '')),
        }
        
        if plant_id:
            headers['X-Epicor-Plant'] = str(plant_id)

        token = config.get('token')
        if token:
            headers['Authorization'] = f'Bearer {token}'
            
        return headers

    def redact_json(self, data: Dict) -> Dict:
        """Standardized redactor with Sanity Check error codes."""
        sensitive_keys = {'Authorization': 'Token', 'X-API-Key': 'Key'}
        redact_marker = "[REDACTED]"
        clean_data = json.loads(json.dumps(data))

        def _scan(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in sensitive_keys:
                        val_str = str(v).strip()
                        if not val_str: raise ValueError(f"ERR_EMPTY_SECRET: {k}")
                        if redact_marker in val_str: raise ValueError(f"ERR_PRE_REDACTED: {k}")
                        # API Key gets a partial mask; Token gets full redaction
                        obj[k] = f"{val_str[:4]}...{val_str[-4:]}" if k == 'X-API-Key' else redact_marker
                    else: _scan(v)
            elif isinstance(obj, list):
                for item in obj: _scan(item)
        _scan(clean_data)
        return clean_data
    
    def get_sensitive_data_patterns(self) -> Dict[str, 're.Pattern']:
        """
        Generates regex patterns for sensitive data stored in the keyring.
        This includes API keys (partial match), hostnames, and a consolidated list of company IDs.
        """
        patterns = {}
        all_companies = set()

        # Use _get_server_dict to get raw config without token status
        servers = self._get_server_dict()
        if not servers:
            return {}

        for name, config in servers.items():
            # Pattern for URL Hostname
            if config.get("url"):
                try:
                    hostname = urlparse(config["url"]).hostname
                    if hostname:
                        patterns[f"URL_HOSTNAME_FOR_{name}"] = re.compile(re.escape(hostname), re.IGNORECASE)
                except Exception:
                    pass  # Ignore if URL parsing fails

            # Pattern for API Key (full and partial match)
            if config.get("api_key"):
                key = config["api_key"]
                if len(key) > 10:
                    first_part = re.escape(key[:5])
                    last_part = re.escape(key[-5:])
                    # Matches base64-like characters in the middle
                    patterns[f"API_KEY_PARTIAL_FOR_{name}"] = re.compile(
                        f'{first_part}[a-zA-Z0-9+/_-]{{10,60}}{last_part}'
                    )
                # Also keep the full key match for exact findings
                patterns[f"API_KEY_FULL_FOR_{name}"] = re.compile(r'\b' + re.escape(key) + r'\b')

            # Collect all company IDs
            companies_str = config.get("companies") or config.get("company") or ""
            if companies_str:
                for company in companies_str.split(','):
                    co = company.strip()
                    if co:
                        all_companies.add(re.escape(co))

        # Create a single, efficient regex for all company IDs
        if all_companies:
            # Sort by length descending to match longer company IDs first
            sorted_companies = sorted(list(all_companies), key=len, reverse=True)
            patterns["ALL_COMPANY_IDS"] = re.compile(r'\b(' + '|'.join(sorted_companies) + r')\b', re.IGNORECASE)

        # Environment nicknames: internal naming that shouldn't appear in shared
        # code/docs (refer to a Kinetic Cloud version instead). The NOTICE_ prefix
        # makes the scanner report these without failing. Case-sensitive, since
        # nicknames are often ordinary words.
        nicknames = sorted({str(n).strip() for n in servers if len(str(n).strip()) >= 3}, key=len, reverse=True)
        if nicknames:
            patterns["NOTICE_ENV_NICKNAMES"] = re.compile(r'\b(' + '|'.join(map(re.escape, nicknames)) + r')\b')

        return patterns

    def panic(self):
        """🚨 Emergency Wipe: Deletes all salts, sentinels, and the cache file."""
        print("🚨 Panic Wipe Initiated...")
        keys_to_purge = ["Sentinel", "Integrity", "SDK_CORE_A", "SDK_CORE_B"]
        for k in keys_to_purge:
            try: keyring.delete_password("KineticSDK", k)
            except: pass
        if os.path.exists(self._cache_file):
            os.remove(self._cache_file)
        print("✅ Security environment purged. Re-run 'store' or 'use' to re-initialize.")

    def clean_sessions(self):
        """Deletes all sessions across all environments (keeps server configs)."""
        print("\n🧹 CLEAN ALL SESSIONS\n")
        print("This will:")
        print("  - Delete all token slots across all environments")
        print("  - Clear the sessions list for all environments")
        print("  - Keep all server configs (URLs, API keys, companies)")
        print("  - Clear the global pointer\n")
        
        if input("Proceed? (y/N): ").strip().lower() != 'y':
            print("Cancelled.")
            return
        
        servers = self._get_server_dict()
        deleted_count = 0
        
        # For each environment, delete all token slots
        for env_name, cfg in servers.items():
            api_key = cfg.get("api_key", "")
            sessions = cfg.get("sessions", [])
            
            for user in sessions:
                secret = f"{api_key}{user.lower()}"
                key_hash = hashlib.sha256(secret.encode()).hexdigest()[:12]
                slot = f"{env_name}-{user.lower()}-{key_hash}"
                try:
                    keyring.delete_password(slot, "current_token")
                    deleted_count += 1
                except:
                    pass
            
            # Clear sessions list but keep the environment config
            cfg["sessions"] = []
            servers[env_name] = cfg
        
        # Save updated config (with empty sessions lists)
        keyring.set_password(SERVICE_SERVERS, "config", json.dumps(servers))
        print(f"✅ Deleted {deleted_count} token slots across all environments")
        print("✅ Cleared sessions lists for all environments")
        print("✅ Server configs preserved (URLs, API keys, companies)")
        
        # Clear global pointer
        try:
            keyring.delete_password("KineticSDK", "LAST_GLOBAL_SESSION")
            print("✅ Cleared LAST_GLOBAL_SESSION pointer")
        except:
            pass

    def reset_pointer(self):
        """Deletes just the global LAST_GLOBAL_SESSION pointer."""
        try:
            keyring.delete_password("KineticSDK", "LAST_GLOBAL_SESSION")
            print("✅ Global Quick-Connect pointer has been reset.")
        except Exception:
            print("⚠️ No pointer found to reset.")

    def full_wipe(self):
        """Nuclear option: Deletes all server configs, tokens, and pointers."""
        print("\n🚨 WARNING: This will delete ALL Kinetic configurations and tokens.")
        if input("Proceed with full wipe? (y/N): ").strip().lower() != 'y':
            print("Aborted.")
            return

        # 1. Clear sessions first (while we still have the config to find the slots)
        self.clean_sessions()

        # 2. Clear pointer
        self.reset_pointer()

        # 3. Delete the master config
        try:
            keyring.delete_password(SERVICE_SERVERS, "config")
            print("✅ Master server configuration deleted.")
        except Exception:
            pass
        print("\n✨ Keyring is now clean. Re-run 'auth store' to begin again.")

    def dump(self):
        """Dumps all raw keyring data to console for debugging (unredacted)."""
        print("=== RAW KEYRING DUMP ===")
        servers = self._get_server_dict()
        print(f"\n[CONFIG: {SERVICE_SERVERS}]\n{json.dumps(servers, indent=2)}")

        last_ptr = keyring.get_password("KineticSDK", "LAST_GLOBAL_SESSION")
        print(f"\n[POINTER: LAST_GLOBAL_SESSION]\nValue: {last_ptr}")

        if last_ptr:
            meta = self._get_token_meta(last_ptr)
            print(f"Pointer Metadata: {json.dumps(meta, indent=2)}")

        print("\n[ACTIVE SESSION SLOTS]")
        for name, cfg in servers.items():
            api_key = cfg.get('api_key', '')
            for user in cfg.get('sessions', []):
                slot = self._get_token_key(name, user, api_key)
                meta = self._get_token_meta(slot)
                status = "EXISTS" if meta else "MISSING"
                print(f"\nSlot: {slot} ({status})")
                if meta:
                    print(json.dumps(meta, indent=2))

    def diagnose(self):
        """
        Provides a safe, redacted summary of all environments and active sessions.
        Replaces scripts/diagnose_keyring_redacted.py.
        """
        servers = self._get_server_dict()
        print("\n--- KEYRING DIAGNOSTIC SUMMARY (REDACTED) ---\n")
        if not servers:
            print("No servers config found.")
            return

        last_ptr = keyring.get_password("KineticSDK", "LAST_GLOBAL_SESSION")

        for name, cfg in servers.items():
            # 1. Setup Redaction Mapping for this env
            companies_raw = cfg.get("companies", "") or cfg.get('company', '')
            co_items = [c.strip() for c in str(companies_raw).split(',') if c.strip()]
            co_map = {f"CO{i}": it for i, it in enumerate(co_items, 1)}
            co_list = list(co_map.keys())

            # 2. Redact Header info
            api_key = cfg.get("api_key", "")
            redacted_key = f"{api_key[:4]}..." if len(api_key) > 4 else "****"

            print(f"Nickname: {name}")
            print(f"  API Key:   {redacted_key}")
            print(f"  Companies: {', '.join(co_list) if co_list else '<none>'}")

            # 3. Session info
            sessions = cfg.get("sessions") or []
            print(f"  Sessions:  {len(sessions)} recorded -> {[self.redact_value(u) for u in sessions]}")

            for u in sessions:
                slot = self._get_token_key(name, u, api_key)
                meta = self._get_token_meta(slot)

                if meta:
                    status = "✅ ACTIVE" if meta.get("_is_valid") else "❌ EXPIRED"
                    rem = meta.get("_remaining", 0)
                    co = meta.get("current_company", "<none>")
                    # Find which COx it is
                    redacted_co = next((k for k, v in co_map.items() if v.upper() == str(co).upper()), "UNK")

                    print(f"    - User: {self.redact_value(meta.get('user_id') or u):<15} | {status} | {str(timedelta(seconds=rem)):<12} | Co: {redacted_co}")
                else:
                    print(f"    - User: {self.redact_value(u):<15} | ⚠️  NO TOKEN")
            print("")

        # 4. Global Pointer info
        print("--- GLOBAL POINTER ---")
        if last_ptr:
            print(f"LAST_GLOBAL_SESSION: ...{last_ptr[-12:].upper()}")
            meta = self._get_token_meta(last_ptr)
            if meta:
                status = "✅ ACTIVE" if meta.get("_is_valid") else "❌ EXPIRED"
                rem = meta.get("_remaining", 0)
                env = meta.get("env_name", "???")
                user = self.redact_value(meta.get("user_id", "???"))
                print(f"  Status: {status}, {str(timedelta(seconds=rem))} left, Env: {env}, User: {user}")
            else:
                print("  Pointer slot exists but no metadata found.")
        else:
            print("No LAST_GLOBAL_SESSION pointer found.")
        print("\nDone.")

    def find_orphans(self, delete: bool = False):
        """
        Heuristic scan for tokens in keyring that aren't in any environment's session list.
        Keyring doesn't support prefix listing, so we check common patterns.
        """
        print("\n🔍 SCANNING FOR ORPHANED TOKENS (Heuristic)\n")

        servers = self._get_server_dict()
        common_users = ["admin", "manager", "api", "svc", "service", "test", "user", "epicor", "kinetic"]
        orphans_found = []

        for name, cfg in servers.items():
            api_key = cfg.get('api_key', '')
            sessions = [s.lower() for s in cfg.get('sessions', [])]

            for user in common_users:
                if user in sessions:
                    continue

                slot = self._get_token_key(name, user, api_key)
                raw = keyring.get_password(slot, "current_token")
                if raw:
                    orphans_found.append((name, user, slot))
                    print(f"❌ ORPHAN FOUND: {name} / {user} -> {slot}")

        if not orphans_found:
            print("✅ No orphaned tokens found using common username patterns.")
            return

        if delete:
            print(f"\n🧹 Purging {len(orphans_found)} orphaned tokens...")
            for _, _, slot in orphans_found:
                try:
                    keyring.delete_password(slot, "current_token")
                    print(f"   ✅ Deleted {slot}")
                except: pass
            print("✨ Cleanup complete.")
        else:
            print(f"\nFound {len(orphans_found)} orphaned tokens. Run with 'find-orphans --delete' to purge them.")

    def validate(self):
        """
        Validate keyring records against schema and auto-repair invalid entries.
        
        Requirements:
        - Each server config must have: url, api_key, companies, sessions (list)
        - Each token slot must have: AccessToken, user_id, _local_timestamp
        - LAST_GLOBAL_SESSION (if exists) must point to valid, non-expired token
        """
        print("\n🔍 VALIDATING KEYRING RECORDS\n")
        
        servers = self._get_server_dict()
        issues_found = []
        fixes_applied = []
        
        # Validate each server config
        for env_name, cfg in list(servers.items()):
            # Check required fields
            if not cfg.get("url"):
                issues_found.append(f"⚠️ {env_name}: missing 'url'")
            if not cfg.get("api_key"):
                issues_found.append(f"⚠️ {env_name}: missing 'api_key'")
            if not cfg.get("companies"):
                issues_found.append(f"⚠️ {env_name}: missing 'companies'")
            
            # Ensure sessions is a list
            if "sessions" not in cfg or not isinstance(cfg.get("sessions"), list):
                cfg["sessions"] = []
                fixes_applied.append(f"🔧 {env_name}: reset sessions to empty list")
            
            # Validate each session's token slot
            api_key = cfg.get("api_key", "")
            sessions = cfg.get("sessions", [])
            valid_sessions = []
            
            for user in sessions:
                secret = f"{api_key}{user.lower()}"
                key_hash = hashlib.sha256(secret.encode()).hexdigest()[:12]
                slot = f"{env_name}-{user.lower()}-{key_hash}"
                
                token_raw = keyring.get_password(slot, "current_token")
                if not token_raw:
                    issues_found.append(f"⚠️ {env_name}: session '{user}' has no token at slot {slot}")
                    continue
                
                try:
                    token_data = json.loads(token_raw)
                    
                    # Check for required token fields
                    if not token_data.get("AccessToken") and not token_data.get("access_token"):
                        issues_found.append(f"⚠️ {env_name}/{user}: token missing AccessToken field")
                        continue
                    
                    if not token_data.get("user_id"):
                        issues_found.append(f"⚠️ {env_name}/{user}: token missing user_id field")
                        continue
                    
                    if not token_data.get("_local_timestamp"):
                        issues_found.append(f"⚠️ {env_name}/{user}: token missing _local_timestamp")
                        continue
                    
                    # Check if token is expired
                    stored_at = token_data.get("_local_timestamp", 0)
                    expires_in = token_data.get("expires_in") or token_data.get("ExpiresIn") or DEFAULT_TTL_SEC
                    remaining = int(expires_in - (time.time() - stored_at))
                    
                    if remaining <= 0:
                        issues_found.append(f"⚠️ {env_name}/{user}: token expired (will be cleaned on next use)")
                    
                    # Token is valid; keep it
                    valid_sessions.append(user)
                    
                except json.JSONDecodeError:
                    issues_found.append(f"⚠️ {env_name}/{user}: token data is corrupted JSON")
                    continue
            
            # Update sessions list to only include valid ones
            if len(valid_sessions) != len(sessions):
                cfg["sessions"] = valid_sessions
                removed = len(sessions) - len(valid_sessions)
                fixes_applied.append(f"🔧 {env_name}: removed {removed} invalid session(s)")
            
            servers[env_name] = cfg
        
        # Validate LAST_GLOBAL_SESSION pointer
        last_ptr = keyring.get_password("KineticSDK", "LAST_GLOBAL_SESSION")
        if last_ptr:
            token_raw = keyring.get_password(last_ptr, "current_token")
            if not token_raw:
                issues_found.append(f"⚠️ LAST_GLOBAL_SESSION points to slot with no token")
                try:
                    keyring.delete_password("KineticSDK", "LAST_GLOBAL_SESSION")
                    fixes_applied.append(f"🔧 Deleted invalid LAST_GLOBAL_SESSION pointer")
                except:
                    pass
            else:
                try:
                    token_data = json.loads(token_raw)
                    stored_at = token_data.get("_local_timestamp", 0)
                    expires_in = token_data.get("expires_in") or token_data.get("ExpiresIn") or DEFAULT_TTL_SEC
                    remaining = int(expires_in - (time.time() - stored_at))
                    if remaining <= 0:
                        issues_found.append(f"⚠️ LAST_GLOBAL_SESSION points to expired token")
                        keyring.delete_password("KineticSDK", "LAST_GLOBAL_SESSION")
                        fixes_applied.append(f"🔧 Deleted expired LAST_GLOBAL_SESSION pointer")
                except json.JSONDecodeError:
                    issues_found.append(f"⚠️ LAST_GLOBAL_SESSION token data corrupted")
                    try:
                        keyring.delete_password("KineticSDK", "LAST_GLOBAL_SESSION")
                        fixes_applied.append(f"🔧 Deleted corrupted LAST_GLOBAL_SESSION pointer")
                    except:
                        pass
        
        # Save repaired config
        if fixes_applied:
            keyring.set_password(SERVICE_SERVERS, "config", json.dumps(servers))
        
        # Print report
        print("=" * 60)
        if issues_found:
            print("\n⚠️ ISSUES FOUND:")
            for issue in issues_found:
                print(f"  {issue}")
        else:
            print("\n✅ No issues found. Keyring is valid.")
        
        if fixes_applied:
            print("\n🔧 REPAIRS APPLIED:")
            for fix in fixes_applied:
                print(f"  {fix}")
        else:
            print("\n✅ No repairs needed.")
        
        print("\n" + "=" * 60)

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Kinetic Config Manager: store Epicor Kinetic environments (URL, API key, companies) in the\n"
            "OS keyring, mint and cache bearer tokens per user, and manage/repair those keyring records.\n"
            "Set KINETIC_VAULT_PASSPHRASE to unlock an encrypted vault non-interactively."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.auth store            # add an environment interactively\n"
            "  python -m kinetic_devops.auth list             # show environments and token status\n"
            "  python -m kinetic_devops.auth use MyTestEnv    # resolve a session, print SET/export lines\n"
            "  python -m kinetic_devops.auth <command> -h     # details for one command"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    subparsers.add_parser(
        "store",
        help="Interactively add or overwrite an environment (nickname, URL, company, API key).",
        description=(
            "Prompts for a nickname, base URL, company ID and a scoped API key (hidden input), then saves "
            "the environment to the keyring after a y/n confirmation. Re-using an existing nickname "
            "overwrites its URL/company/API key but keeps its recorded sessions."
        ),
    )
    subparsers.add_parser(
        "list",
        help="List saved environments with redacted URL, companies and token status.",
        description=(
            "Read-only. Prints one row per saved environment: nickname, URL (hostname redacted), company "
            "list and whether a valid cached token exists (minutes left / Expired / Empty)."
        ),
    )
    subparsers.add_parser(
        "migrate",
        help="Rewrite stored environments into the current schema (legacy 'company' -> 'companies').",
        description=(
            "Rewrites the stored environments map: moves a legacy 'company' field into 'companies' "
            "(defaulting to 'ACME' when neither is set), adds a 'display_name', and drops entries that "
            "are not objects. Writes the result back to the keyring."
        ),
    )
    subparsers.add_parser(
        "encrypt-vault",
        help="Encrypt the stored environments map and cached tokens with a passphrase.",
        description=(
            "Encrypts the environments map and every recorded session's cached token in the keyring "
            "(PBKDF2 + AES). Uses KINETIC_VAULT_PASSPHRASE, or prompts twice for a new passphrase in an "
            "interactive terminal. Afterwards every tool needs the passphrase to read the vault; losing "
            "it makes the stored data unreadable."
        ),
    )
    subparsers.add_parser(
        "decrypt-vault",
        help="Decrypt the vault back to plain JSON in the keyring.",
        description=(
            "Decrypts the environments map and recorded sessions' cached tokens and stores them back in "
            "the keyring as plain JSON. Uses KINETIC_VAULT_PASSPHRASE or prompts for the passphrase."
        ),
    )
    subparsers.add_parser(
        "panic",
        help="DESTRUCTIVE: delete the SDK's redaction salts/sentinels and local redaction cache file.",
        description=(
            "DESTRUCTIVE: deletes the KineticSDK keyring entries Sentinel, Integrity, SDK_CORE_A and "
            "SDK_CORE_B and removes the local redaction cache file (~/.k_red_cache.dat). Environments and "
            "tokens are not touched. No confirmation prompt."
        ),
    )
    subparsers.add_parser(
        "validate",
        help="Check keyring records for missing fields/expired tokens and auto-repair them.",
        description=(
            "Checks each environment for url/api_key/companies and each recorded session's cached token. "
            "Repairs in place: removes sessions whose token is missing, corrupt or incomplete from the "
            "session list, and deletes the last-session pointer if it is missing, expired or corrupt. "
            "Prints the issues found and repairs applied."
        ),
    )
    subparsers.add_parser(
        "clean-sessions",
        help="DESTRUCTIVE: delete all cached tokens and session lists (keeps environment configs).",
        description=(
            "DESTRUCTIVE: after a y/N confirmation, deletes every recorded session's cached token across "
            "all environments, empties each environment's session list and clears the last-session "
            "pointer. URLs, API keys and companies are kept; you will need to log in again."
        ),
    )
    set_key_p = subparsers.add_parser(
        "set-api-key",
        help="Replace the API key for an existing environment and drop its cached tokens.",
        description=(
            "Replaces only the API key of an existing environment. If the key changes, the cached tokens "
            "for that environment's sessions (stored under the old key) are deleted and the last-session "
            "pointer is cleared if it pointed at one of them; run 'renew' afterwards."
        ),
    )
    set_key_p.add_argument("env", metavar="ENV", help="Environment nickname (case-insensitive).")
    set_key_p.add_argument(
        "--value",
        default="",
        metavar="API_KEY",
        help="New API key. Omit to be prompted with hidden input (preferred: avoids shell history).",
    )
    renew_p = subparsers.add_parser(
        "renew",
        help="Mint a fresh bearer token for an environment/user and print SET/export lines.",
        description=(
            "Fetches a new token from the environment's TokenResource endpoint (prompts for the Epicor\n"
            "password), verifies it with a UserFile probe, caches it in the keyring, then prints the same\n"
            "shell lines as 'use'. With --from-token it first tries to renew from the cached bearer token\n"
            "instead of a password. A missing ENV or user is chosen interactively."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.auth renew MyTestEnv\n"
            "  python -m kinetic_devops.auth renew MyTestEnv --from-token --allow-password-fallback"
        ),
    )
    renew_p.add_argument(
        "env", nargs="?", default="", metavar="ENV",
        help="Environment nickname. Omit to pick (or reuse the last session) interactively.",
    )
    renew_p.add_argument(
        "--user", default="", metavar="USER_ID",
        help="Epicor user ID. Default: picked interactively, or the first recorded session for ENV.",
    )
    renew_p.add_argument(
        "--co", default="", metavar="COMPANY",
        help="Company ID for the session. Default: the session's current company, else the first configured.",
    )
    renew_p.add_argument(
        "--from-token", action="store_true",
        help="Renew using the existing cached bearer token instead of prompting for a password.",
    )
    renew_p.add_argument(
        "--allow-password-fallback", action="store_true",
        help="With --from-token: if token renewal fails, fall back to the password prompt.",
    )

    subparsers.add_parser(
        "reset-pointer",
        help="Clear the 'last active session' quick-connect pointer.",
        description=(
            "Deletes only the LAST_GLOBAL_SESSION keyring pointer, so the next tool run without an "
            "explicit environment prompts for one instead of auto-reusing the last session."
        ),
    )
    subparsers.add_parser(
        "full-wipe",
        help="DESTRUCTIVE: delete ALL environments, cached tokens and the session pointer.",
        description=(
            "DESTRUCTIVE / IRREVERSIBLE: after confirmation, runs clean-sessions (which asks again), "
            "resets the pointer and deletes the master environments config from the keyring. You must "
            "re-run 'store' for every environment afterwards."
        ),
    )
    subparsers.add_parser(
        "dump",
        help="PRINTS SECRETS: dump the raw keyring config and token metadata, unredacted.",
        description=(
            "Debug aid. PRINTS SECRETS UNREDACTED to the console: the full environments map (including "
            "API keys), the last-session pointer and every cached token's metadata (including bearer "
            "tokens). Do not run where output is logged or shared; prefer 'diagnose'."
        ),
    )
    subparsers.add_parser(
        "diagnose",
        help="Print a redacted summary of environments, sessions and the session pointer.",
        description=(
            "Read-only, redacted alternative to 'dump': for each environment shows a truncated API key, "
            "companies as CO1/CO2/..., redacted user IDs with token status and time left, plus the state "
            "of the last-session pointer."
        ),
    )

    orphan_p = subparsers.add_parser(
        "find-orphans",
        help="Scan for tokens not associated with any config",
        description=(
            "Heuristic scan: for each environment, checks token slots for common user names (admin, api, "
            "svc, test, ...) that are not in its session list and reports any that hold a token. The "
            "keyring cannot be listed, so orphans under other user names are not found."
        ),
    )
    orphan_p.add_argument("--delete", action="store_true", help="Purge found orphans")
    sync_p = subparsers.add_parser(
        "sync-companies",
        help="Compare the server's authorized companies for your user with the local list and update it.",
        description=(
            "Calls Erp.BO.UserFileSvc/GetRows on the environment (reusing or minting a session) to read the "
            "companies your user is authorized for. On a mismatch, prompts to overwrite, merge or keep the "
            "local company list, then saves the choice to the keyring."
        ),
    )
    sync_p.add_argument("env", metavar="ENV", help="Environment nickname (case-insensitive).")

    per_env_cmds = {
        "inspect": (
            "Show one environment's redacted config and per-user session status.",
            "Read-only: prints the environment's URL, a truncated API key, its companies and, for each "
            "recorded user, whether the cached token is active/expired, time left and current company.",
        ),
        "use": (
            "Resolve a session and print SET/export lines that activate it for other tools.",
            "Resolves a session for ENV (logging in and prompting for the password if no valid token is\n"
            "cached) and prints shell lines setting KIN_COMPANY, KINETIC_SESSION_ENV, KINETIC_SESSION_USER\n"
            "and KINETIC_SESSION_CO. Also records it as the last-active session. Without ENV, reuses the\n"
            "last session or prompts.",
        ),
        "delete": (
            "DESTRUCTIVE: remove a saved environment from the keyring.",
            "DESTRUCTIVE: deletes the environment entry (URL, API key, companies, session list) from the "
            "stored config, with no confirmation prompt. Its cached token slots are not deleted; run "
            "'logout ENV' first to clear them.",
        ),
        "logout": (
            "Delete all cached tokens for one environment and clear its session list.",
            "Deletes the cached token of every recorded user for ENV and empties its session list. The "
            "environment config itself is kept.",
        ),
    }
    for cmd in ["inspect", "use", "delete", "logout"]:
        cmd_help, cmd_desc = per_env_cmds[cmd]
        if cmd == "use":
            p = subparsers.add_parser(
                cmd,
                help=cmd_help,
                description=cmd_desc,
                formatter_class=argparse.RawDescriptionHelpFormatter,
                epilog=(
                    "examples:\n"
                    "  python -m kinetic_devops.auth use MyTestEnv\n"
                    "  set KIN_USER=jdoe & python -m kinetic_devops.auth use MyTestEnv   (Windows cmd)"
                ),
            )
        else:
            p = subparsers.add_parser(cmd, help=cmd_help, description=cmd_desc)
        if cmd == "use":
            p.add_argument(
                "env", nargs="?", default=None, metavar="ENV",
                help="Environment nickname. Omit to reuse the last active session or pick interactively.",
            )
            p.add_argument(
                "--user",
                default="",
                help="Disambiguate which session to use for this environment. "
                     "Prefer the KIN_USER environment variable instead -- this "
                     "flag can leak into shell history and process listings.",
            )
        else:
            p.add_argument("env", metavar="ENV", help="Environment nickname (case-insensitive).")

    rename_p = subparsers.add_parser(
        "rename",
        help="Rename a saved environment, keeping its stored credentials and cached sessions.",
        description=textwrap.fill(
            "Renames environment OLD to NEW without re-entering anything: the config entry (URL, API key, "
        "companies, session list) moves to NEW, each user's cached token slot is copied to its new slot name "
        "unchanged (encrypted slots stay encrypted), the last-session pointer follows, and only then are the old "
        "slots removed. An encrypted vault is re-encrypted with the same passphrase. Anything outside the "
        "keyring that refers to OLD (scripts, KINETIC_SESSION_ENV, environment repos) is not changed.",
            width=100,
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.auth rename OldName NewName --dry-run\n"
            "  python -m kinetic_devops.auth rename OldName NewName"
        ),
    )
    rename_p.add_argument("old", metavar="OLD", help="Current environment nickname (case-insensitive).")
    rename_p.add_argument("new", metavar="NEW", help="New nickname (no whitespace; must not already exist).")
    rename_p.add_argument("--dry-run", action="store_true", help="Show what would move without changing anything.")

    ai_set_p = subparsers.add_parser(
        "ai-set",
        help="Create or update a named AI server profile (URL, API style, model) and store its API key in the keyring.",
        description=textwrap.fill(
            "Stores a named AI server profile used by kinetic_devops.analysis in the OS keyring: base URL, "
            "API style (openwebui for an Open WebUI server, openai for a plain OpenAI-compatible /v1 server) "
            "and an optional default model. Several profiles can point at the same server with different "
            "models (e.g. fast and deep). The API key is prompted with hidden input and stored once per server "
            "URL; it is never accepted on the command line or printed. Leave it blank to keep the stored one. "
            "The first profile becomes the default.",
            width=100,
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  uv run python -m kinetic_devops.auth ai-set --name deep --url http://ai-host:3000 --api-style openwebui --model local/deep --default\n"
            "  uv run python -m kinetic_devops.auth ai-set --name fast --url http://ai-host:3000 --api-style openwebui --model local/fast\n"
            "  uv run python -m kinetic_devops.auth ai-show"
        ),
    )
    ai_set_p.add_argument("--name", default="", metavar="NAME", help="Profile name (default: the current default profile, or 'default').")
    ai_set_p.add_argument("--url", default="", metavar="URL", help="AI server base URL (prompted if omitted).")
    ai_set_p.add_argument(
        "--api-style", choices=AI_API_STYLES, default=None,
        help="openwebui: /api/models + /api/chat/completions; openai: /v1/models + /v1/completions "
        "(prompted if omitted; default openwebui).",
    )
    ai_set_p.add_argument("--model", default=None, metavar="MODEL", help="Default model ID (blank = first listed by the server).")
    ai_set_p.add_argument("--default", action="store_true", dest="make_default", help="Make this the default profile.")
    subparsers.add_parser(
        "ai-show",
        help="List the stored AI server profiles (never the keys themselves).",
        description="Lists every stored AI profile -- base URL, API style, default model, whether an API key "
        "is stored for its URL -- and marks the default one.",
    )
    ai_default_p = subparsers.add_parser(
        "ai-default", help="Choose which AI profile is used by default.",
        description="Makes NAME the default AI profile (used when analysis is run without --ai-profile).",
    )
    ai_default_p.add_argument("name", metavar="NAME", help="Existing profile name.")
    ai_clear_p = subparsers.add_parser(
        "ai-clear",
        help="DESTRUCTIVE: delete one AI profile (--name) or all of them, with their API keys.",
        description="DESTRUCTIVE: removes the named AI profile, or every profile when --name is omitted. A "
        "server's API key is removed once no remaining profile uses that URL.",
    )
    ai_clear_p.add_argument("--name", default=None, metavar="NAME", help="Profile to delete (default: all profiles).")

    args = parser.parse_args()
    try:
        mgr = KineticConfigManager(debug=True)
        if args.command == "store": mgr.store()
        elif args.command == "list": mgr.list()
        elif args.command == "migrate": mgr.migrate()
        elif args.command == "encrypt-vault":
            # Encrypt servers map and all token slots.
            passphrase = os.environ.get("KINETIC_VAULT_PASSPHRASE")
            if not passphrase and sys.stdout.isatty():
                passphrase = getpass.getpass("New vault passphrase: ")
                confirm = getpass.getpass("Confirm vault passphrase: ")
                if passphrase != confirm:
                    print("Passphrases do not match. Aborting.")
                    sys.exit(1)

            if not passphrase:
                print("Provide KINETIC_VAULT_PASSPHRASE env var or run interactively.")
                sys.exit(1)

            # Encrypt servers map
            raw = keyring.get_password(SERVICE_SERVERS, "config")
            if not raw:
                print("No servers config to encrypt.")
            else:
                if crypto.is_encrypted_blob(raw):
                    print("Vault already encrypted.")
                else:
                    try:
                        servers = json.loads(raw)
                        enc = crypto.encrypt_json(servers, passphrase)
                        keyring.set_password(SERVICE_SERVERS, "config", enc)
                        print("✅ Servers map encrypted.")
                    except Exception as e:
                        print(f"Failed to encrypt servers: {e}")

            # Encrypt token slots
            servers = mgr._get_server_dict()
            for name, cfg in servers.items():
                api_key = cfg.get('api_key', '')
                for user in cfg.get('sessions', []):
                    slot = mgr._get_token_key(name, user, api_key)
                    token_raw = keyring.get_password(slot, 'current_token')
                    if token_raw and not crypto.is_encrypted_blob(token_raw):
                        try:
                            token_data = json.loads(token_raw)
                            enc_token = crypto.encrypt_json(token_data, passphrase)
                            keyring.set_password(slot, 'current_token', enc_token)
                        except Exception:
                            pass
            print("✅ Vault encryption completed.")

        elif args.command == "decrypt-vault":
            passphrase = os.environ.get("KINETIC_VAULT_PASSPHRASE")
            if not passphrase and sys.stdout.isatty():
                passphrase = getpass.getpass("Vault passphrase: ")
            if not passphrase:
                print("Provide KINETIC_VAULT_PASSPHRASE env var or run interactively.")
                sys.exit(1)

            raw = keyring.get_password(SERVICE_SERVERS, "config")
            if not raw:
                print("No servers config found.")
            else:
                if not crypto.is_encrypted_blob(raw):
                    print("Servers map is not encrypted.")
                else:
                    try:
                        dec = crypto.decrypt_json(raw, passphrase)
                        keyring.set_password(SERVICE_SERVERS, "config", json.dumps(dec))
                        print("✅ Servers map decrypted (now stored as plain JSON).")
                    except Exception as e:
                        print(f"Failed to decrypt servers: {e}")

            # Decrypt token slots
            servers = mgr._get_server_dict()
            for name, cfg in servers.items():
                api_key = cfg.get('api_key', '')
                for user in cfg.get('sessions', []):
                    slot = mgr._get_token_key(name, user, api_key)
                    token_raw = keyring.get_password(slot, 'current_token')
                    if token_raw and crypto.is_encrypted_blob(token_raw):
                        try:
                            token_data = crypto.decrypt_json(token_raw, passphrase)
                            keyring.set_password(slot, 'current_token', json.dumps(token_data))
                        except Exception:
                            pass
            print("✅ Vault decryption completed.")

        elif args.command == "panic": mgr.panic()
        elif args.command == "validate": mgr.validate()
        elif args.command == "clean-sessions": mgr.clean_sessions()
        elif args.command == "set-api-key": mgr.set_api_key(args.env, args.value)
        elif args.command == "renew":
            mgr.renew(
                args.env,
                args.user,
                args.co,
                from_token=bool(args.from_token),
                allow_password_fallback=bool(args.allow_password_fallback),
            )
        elif args.command == "reset-pointer": mgr.reset_pointer()
        elif args.command == "full-wipe": mgr.full_wipe()
        elif args.command == "dump": mgr.dump()
        elif args.command == "diagnose": mgr.diagnose()
        elif args.command == "find-orphans": mgr.find_orphans(delete=args.delete)
        elif args.command == "sync-companies": mgr.sync_companies(args.env)
        elif args.command == "inspect": mgr.inspect(args.env)
        elif args.command == "use": mgr.use(args.env, user=args.user)
        elif args.command == "delete": mgr.delete(args.env)
        elif args.command == "ai-set":
            default_name, profiles = list_ai_profiles()
            name = args.name or default_name or AI_DEFAULT_PROFILE
            # Editing an existing profile defaults to its values; a new one to the default profile's server.
            current = profiles.get(name) or profiles.get(default_name) or {}
            url = args.url or input(f"AI base URL [{current.get('base_url', '')}]: ").strip() or current.get("base_url", "")
            style = args.api_style or (input(f"API style {AI_API_STYLES} [{current.get('api_style', 'openwebui')}]: ").strip()
                                       or current.get("api_style", "openwebui"))
            existing_model = (profiles.get(name) or {}).get("model", "")
            model = args.model if args.model is not None else (
                input(f"Default model (blank = first available) [{existing_model}]: ").strip() or existing_model)
            had_key = bool(get_ai_api_key(url))
            key = getpass.getpass("API key (hidden; blank keeps the stored one): " if had_key else "API key (hidden): ").strip()
            profile = set_ai_profile(url, style, model, api_key=key or None, name=name, make_default=args.make_default)
            stored = "updated" if key else ("kept" if had_key else "none stored")
            is_default = " (default)" if list_ai_profiles()[0] == name else ""
            print(f"✅ AI profile '{name}'{is_default} saved: {profile['base_url']} ({profile['api_style']}), "
                  f"model={profile['model'] or '<first available>'}, key {stored}.")
        elif args.command == "ai-show":
            default_name, profiles = list_ai_profiles()
            if not profiles:
                print("No AI profile stored. Run: uv run python -m kinetic_devops.auth ai-set")
            for pname, profile in profiles.items():
                has_key = "yes" if get_ai_api_key(profile.get("base_url", "")) else "no"
                print(f"{'*' if pname == default_name else ' '} {pname}: {profile.get('base_url')} "
                      f"({profile.get('api_style')}), model={profile.get('model') or '<first available>'}, api key: {has_key}")
        elif args.command == "ai-default":
            set_default_ai_profile(args.name)
            print(f"✅ Default AI profile: {args.name}")
        elif args.command == "ai-clear":
            target = f"AI profile '{args.name}'" if args.name else "All AI profiles"
            print(f"✅ {target} removed." if clear_ai_profile(args.name) else "Nothing stored.")
        elif args.command == "rename":
            result = mgr.rename(args.old, args.new, dry_run=args.dry_run)
            verb = "Would rename" if result["dry_run"] else "✅ Renamed"
            print(f"{verb} '{result['old']}' -> '{result['new']}': "
                  f"{len(result['sessions_moved'])} cached session(s) moved"
                  f"{', last-session pointer updated' if result['pointer_moved'] else ''}.")
            if result["sessions_without_cached_token"]:
                print(f"   No cached token for: {', '.join(result['sessions_without_cached_token'])} "
                      "(their session entries move with the config).")
            if not result["dry_run"]:
                print(f"   Update anything outside the keyring that still says '{result['old']}' "
                      "(scripts, KINETIC_SESSION_ENV, environment repos).")
        elif args.command == "logout": mgr.logout(args.env)
        else: parser.print_help()
    except Exception as e:
        print(f"❌ Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()