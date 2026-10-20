import hashlib
import binascii
import zlib
import pickle
import base64
import re
import os
import shutil
from datetime import datetime, timezone
import getpass
from kinetic_devops import _lazy_import

# keyring.errors is resolved on first use (only when a keyring call fails), so
# the lazy import stays lazy; an explicit `import keyring.errors` would load
# keyring eagerly.
keyring = _lazy_import("keyring")
from typing import Optional, Dict, Set
from urllib.parse import urlsplit, urlunsplit
from .fs_ops import safe_atomic_write_text, describe_overwrite_risk

class KineticCore:
    def __init__(self, debug: bool = False):
        self.debug = debug
        self._os_user = getpass.getuser()
        self._cache_file = os.path.expanduser("~/.k_red_cache.dat")
        
        # 1. Retrieve Dual Salts (The only root entropy)
        self._salt_a = self._get_or_init_salt("SDK_CORE_A")
        self._salt_b = self._get_or_init_salt("SDK_CORE_B")
        
        # 2. Derive Domain-Specific Keys (Sliding Window - Zero Constants)
        self._key_header = self._derive_sliding(0)
        self._key_scramble = self._derive_sliding(11)
        self._key_ids = self._derive_sliding(22)

        # 3. Security-Hardened RAM Placeholders
        self._hex_scrambled_blob: Optional[str] = None  # Obscured even in RAM
        self._cached_pattern: Optional[re.Pattern] = None

    def _get_or_init_salt(self, name: str) -> str:
        try:
            val = keyring.get_password("KineticSDK", name)
        except keyring.errors.NoKeyringError as e:
            # Headless Linux (SSH sessions, CI, containers) usually has no
            # Secret Service, so keyring has no backend. Fail with guidance
            # instead of a raw traceback.
            raise RuntimeError(
                "No keyring backend available, so credentials can't be stored "
                "or read. On headless Linux, run inside a desktop session with "
                "a Secret Service (e.g. gnome-keyring) or configure a keyring "
                "backend -- see https://pypi.org/project/keyring."
            ) from e
        if not val:
            import secrets
            val = secrets.token_hex(32)
            keyring.set_password("KineticSDK", name, val)
        return val

    def _derive_sliding(self, window_start: int) -> bytes:
        """Uses Salt_B as a map to sample and transform Salt_A."""
        a, b = self._salt_a.encode(), self._salt_b.encode()
        length = len(a)
        derived = []
        for i in range(length):
            idx = (window_start + i + b[i % len(b)]) % length
            derived.append(a[idx] ^ b[(i + window_start) % len(b)])
        return hashlib.sha256(bytes(derived)).digest()

    def _fast_scramble(self, data: bytes) -> bytes:
        """XORs binary data using the Derived Scramble Key."""
        return bytes(b ^ self._key_scramble[i % len(self._key_scramble)] for i, b in enumerate(data))

    def debug_log(self, message: str):
        """Log a debug message if debug mode is enabled."""
        if self.debug:
            print(f"[DEBUG] {message}")

    def _obfuscate(self, value: str) -> str:
        """Identity hash for logs using the Derived ID Key."""
        h = hashlib.sha256(self._key_ids + str(value).encode()).digest()
        return f"ID_{base64.b64encode(h).decode()[:6]}"

    def redact_value(self, value: str) -> str:
        """Public alias for the internal _obfuscate method."""
        return self._obfuscate(value)

    def _generate_magic_marker(self) -> str:
        """Header fingerprint: SHA256 of the Derived Header Key."""
        return hashlib.sha256(self._key_header).hexdigest()

    def _ensure_redaction_ready(self):
        """Validates Sentinel and Integrity before loading/rebuilding."""
        if self._cached_pattern and self._hex_scrambled_blob:
            return

        # Check Config State
        current_config_hash = self.mgr._get_config_hash() if hasattr(self, 'mgr') else "0"
        stored_sentinel = keyring.get_password("KineticSDK", "Sentinel")
        stored_integrity = keyring.get_password("KineticSDK", "Integrity")

        # 1. THE FAST PATH (Verified Load)
        if os.path.exists(self._cache_file) and current_config_hash == stored_sentinel:
            try:
                with open(self._cache_file, "r") as f:
                    full_blob = f.read().strip()
                
                # Check Magic Marker (Salt alignment)
                marker = self._generate_magic_marker()
                if not full_blob.startswith(marker):
                    raise ValueError("Salt mismatch")

                # Check Integrity (Tamper check)
                if hashlib.sha256(full_blob.encode()).hexdigest() != stored_integrity:
                    raise ValueError("Integrity breach")

                self._hex_scrambled_blob = full_blob[len(marker):]
                # Ephemeral expansion for pattern only
                binary = self._fast_scramble(binascii.unhexlify(self._hex_scrambled_blob))
                self._cached_pattern = pickle.loads(zlib.decompress(binary))['pattern']
                return
            except Exception as e:
                if self.debug: print(f"⚠️ Cache Load Failed: {e}")

        # 2. THE HEAVY PATH (Rebuild)
        self._rebuild_secure_cache(current_config_hash)

    def _rebuild_secure_cache(self, config_hash: str):
        if self.debug: print("⚙️  Rebuilding Security Engine...")
        
        # Build Map
        redaction_map = self._build_global_redaction_map()
        
        # Compile Pattern
        sorted_keys = sorted(redaction_map.keys(), key=len, reverse=True)
        pattern_str = rf"\b({'|'.join(re.escape(v) for v in sorted_keys)})\b"
        self._cached_pattern = re.compile(pattern_str, flags=re.IGNORECASE)

        # Create Dark Blob
        binary_blob = pickle.dumps({'map': redaction_map, 'pattern': self._cached_pattern})
        scrambled = self._fast_scramble(zlib.compress(binary_blob))
        payload_hex = binascii.hexlify(scrambled).decode()
        
        full_blob = self._generate_magic_marker() + payload_hex
        integrity_hash = hashlib.sha256(full_blob.encode()).hexdigest()

        # Update Keyring and Disk
        keyring.set_password("KineticSDK", "Sentinel", config_hash)
        keyring.set_password("KineticSDK", "Integrity", integrity_hash)

        # Selected state/cache safeguard: if cache file is in-repo and risky,
        # preserve a timestamped backup before atomic replacement.
        if os.path.exists(self._cache_file):
            try:
                risk = describe_overwrite_risk(self._cache_file)
                in_repo = bool(risk.get("in_repo"))
                risk_level = str(risk.get("risk_level", "none")).lower()
                if in_repo and risk_level in {"high", "critical"}:
                    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    backup_path = f"{self._cache_file}.{ts}.bak"
                    shutil.copy2(self._cache_file, backup_path)
                    if self.debug:
                        print(f"⚠️ Cache overwrite safeguard: backup created at {backup_path}")
            except Exception:
                if self.debug:
                    print("⚠️ Cache overwrite safeguard skipped due to backup error")

        safe_atomic_write_text(self._cache_file, full_blob)
            
        self._hex_scrambled_blob = payload_hex

    def _build_global_redaction_map(self) -> Dict[str, str]:
        """Gathers sensitive values from Manager and obfuscates them."""
        rmap = {}
        if hasattr(self, 'mgr'):
            configs = self.mgr._get_server_dict()
            for env, cfg in configs.items():
                rmap[env] = self._obfuscate(env)
                for user in cfg.get('sessions', []):
                    rmap[user] = self._obfuscate(user)
                cos = str(cfg.get('company', '')).split(',')
                for co in cos:
                    if co.strip(): rmap[co.strip()] = self._obfuscate(co.strip())
        return rmap

    def _deep_scrub(self, text: str) -> str:
        """Expanding the map only for the duration of the scrub."""
        if not text: return text
        self._ensure_redaction_ready()

        # Ephemeral Hydration
        binary = zlib.decompress(self._fast_scramble(binascii.unhexlify(self._hex_scrambled_blob)))
        temp_map = pickle.loads(binary)['map']

        def _rep(m):
            found = m.group(0).lower()
            for k, v in temp_map.items():
                if k.lower() == found: return v
            return m.group(0)

        clean = self._cached_pattern.sub(_rep, text)
        temp_map.clear() # Immediate purge
        return clean

    def _heuristic_redact(self, text: str) -> str:
        """
        Applies heuristic regex to redact sensitive JSON fields in logs.
        Matches keys like 'Company', 'Password', 'Token', etc.
        Handles both normal JSON ("key": "val") and escaped/stringified JSON (\"key\":\"val\").
        """
        if not text: return text

        # Keywords — any key whose name starts with one of these is redacted
        keywords = (
            r"SysRow|job|part|desc|pay|pass|doc|path|vend|addr|auth|ship|cust|plant|company|comp|"
            r"user|email|phone|contact|acct|tax|amt|price|host|api|key|token|sec|order|inv|"
            r"pack|po|quote|check|serial|lot|comment|note|entity|code"
        )

        # Suffixes (optional)
        suffixes = r"(?:[\-\s_]*(?:id|name|num|ref|val|short|full|title|author))?"

        # Quote helpers: handle both plain " and escaped \" (stringified JSON)
        quote     = r'(?:\\?[\"\'])'   # required quote (plain or backslash-escaped)
        quote_opt = r'(?:\\?[\"\'])?'  # optional quote

        # Pattern: Group 1 (Key + separator + opening value quote), Group 2 (value)
        pattern = (
            r'(?i)'                                  # Case insensitive
            r'('                                     # Group 1 start
            + quote_opt +                            # Optional opening key quote
            r'(?:' + keywords + r')'                 # Keyword match
            + suffixes +                             # Optional suffixes
            r'[^:\\\"\']*'                           # Chars between keyword and closing key quote
            + quote_opt +                            # Optional closing key quote
            r'\s*:\s*'                               # Colon separator (whitespace tolerant)
            + quote +                                # Required opening value quote
            r')'                                     # Group 1 end
            r'(.*?)'                                 # Group 2: value (non-greedy)
            r'(?='                                   # Lookahead (zero-width)
            + quote +                                # Closing value quote
            r'(?:\s*[,}\]\r\n]|\s*$)'               # Followed by delimiter or end of string
            r')'
        )

        try:
            return re.sub(pattern, r'\1[REDACTED]', text)
        except Exception:
            return text

    def _redact_url_fragments(self, text: str) -> str:
        """Replace any absolute URLs embedded in free text with a placeholder."""
        if not text:
            return text
        return re.sub(r'https?://[^\s"\'<>]+', '[REDACTED_URL]', text)

    def _redact_url(self, url: str) -> str:
        """Redact proprietary URL components while preserving endpoint shape."""
        if not url:
            return url
        try:
            parts = urlsplit(str(url))
            if not parts.scheme or not parts.netloc:
                return '[REDACTED_URL]'

            path = re.sub(
                r'(?i)(/api/v\d+/(?:odata|efx)/)([^/?#]+)',
                r'\1[REDACTED_COMPANY]',
                parts.path,
            )
            query = '[REDACTED_QUERY]' if parts.query else ''
            fragment = '[REDACTED_FRAGMENT]' if parts.fragment else ''
            return urlunsplit((parts.scheme, '[REDACTED_HOST]', path, query, fragment))
        except Exception:
            return '[REDACTED_URL]'

    def _redact_runtime_values(self, text: str) -> str:
        """Redact active runtime identity values that may appear in free-form messages."""
        if not text or not hasattr(self, 'config') or not isinstance(self.config, dict):
            return text

        replacements = []
        url = self.config.get('url', '')
        if url:
            try:
                parts = urlsplit(str(url))
                replacements.extend([
                    parts.netloc,
                    parts.hostname or '',
                    parts.path.strip('/'),
                ])
            except Exception:
                replacements.append(str(url))

        replacements.extend([
            self.config.get('company', ''),
            self.config.get('user_id', ''),
            self.config.get('nickname', ''),
        ])

        cleaned = text
        for value in sorted({str(item) for item in replacements if item}, key=len, reverse=True):
            cleaned = re.sub(re.escape(value), '[REDACTED]', cleaned, flags=re.IGNORECASE)
        return cleaned

    def _sanitize_log_text(self, text: str) -> str:
        """Apply zero-trust sanitization before printing any free-form text."""
        if not text:
            return text
        return self._heuristic_redact(self._redact_runtime_values(self._redact_url_fragments(text)))

    def _redact_headers(self, headers: Dict) -> Dict:
        """Redact sensitive header values before they reach stdout."""
        if not headers:
            return {}

        sensitive_key = re.compile(
            r'(?i)^(authorization|x-api-key|x-company|x-epicor-company|cookie|set-cookie|x-user|x-user-id|x-token|host)$'
        )

        safe_headers = {}
        for key, value in headers.items():
            if sensitive_key.search(str(key)):
                if str(key).lower() == 'authorization' and isinstance(value, str) and value.startswith('Bearer '):
                    safe_headers[key] = 'Bearer [REDACTED]'
                else:
                    safe_headers[key] = '[REDACTED]'
            elif isinstance(value, str):
                safe_headers[key] = self._sanitize_log_text(value)
            else:
                safe_headers[key] = value
        return safe_headers

    def build_headers(self, token: str, api_key: str, company: str) -> Dict[str, str]:
        """Standardized header builder for Kinetic API calls."""
        import json
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "X-Company": company
        }

    def log_wire(self, method: str, url: str, headers: Dict, body=None, resp=None):
        """Redacted wire logging for debugging HTTP traffic."""
        if not self.debug and (not resp or resp.ok):
            return
        
        import json
        print("\n" + "="*60)
        print("🔍 WIRE LOG (REDACTED)")
        print(f"Method: {method}")
        print(f"URL: {self._redact_url(url)}")
        
        # Redact sensitive headers
        if headers:
            safe_headers = self._redact_headers(headers)
            try:
                print(f"Headers: {json.dumps(safe_headers, indent=2)}")
            except:
                print(f"Headers: {safe_headers}")
        
        # Log Body (Redacted)
        if body:
            body_str = json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body)
            print(f"Request Body: {self._sanitize_log_text(body_str)}")

        # Log response status
        if resp:
            print(f"Status: {resp.status_code} {resp.reason if hasattr(resp, 'reason') else 'Unknown'}")
            if not resp.ok:
                try:
                    err_body = resp.json()
                    err_str = json.dumps(err_body, indent=2)
                    print(f"Error Body: {self._sanitize_log_text(err_str)}")
                except:
                    print(f"Body: {self._sanitize_log_text(resp.text[:500] if resp.text else 'empty')}")
        
        print("="*60 + "\n")
        