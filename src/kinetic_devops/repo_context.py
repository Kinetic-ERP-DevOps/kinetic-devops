"""Shared repository host context and token resolution helpers.

This module centralizes:
- Git remote parsing and provider detection
- Forgejo API base normalization
- Keyring token lookup strategy for multi-repo/multi-owner setups
"""

from __future__ import annotations

import os
import subprocess
from typing import Dict, List, Tuple
from urllib.parse import urlparse


_SSH_SCHEMES = ("ssh", "git+ssh", "ssh+git")


def _owner_repo(path: str, value: str, error_type: type[Exception]) -> Tuple[str, str]:
    owner_repo = path.rstrip("/")
    if owner_repo.endswith(".git"):
        owner_repo = owner_repo[:-4]
    parts = [p for p in owner_repo.split("/") if p]
    if len(parts) < 2:
        raise error_type(f"Unable to parse owner/repo from git remote: {redact_url(value)}")
    return parts[-2], parts[-1]


def redact_url(value: str) -> str:
    """Return URL with any user info (user[:password]@) removed, for error messages and logs."""
    text = str(value or "")
    if "://" in text:
        scheme, rest = text.split("://", 1)
        authority, sep, tail = rest.partition("/")
        if "@" in authority:
            authority = authority.rsplit("@", 1)[1]
        return f"{scheme}://{authority}{sep}{tail}"
    # SCP-like: [user@]host:path
    head, sep, tail = text.partition("/")
    if "@" in head:
        head = head.rsplit("@", 1)[1]
    return f"{head}{sep}{tail}"


def parse_git_remote(url: str, error_type: type[Exception] = RuntimeError) -> Dict[str, str]:
    """Parse git remote URL and extract host/owner/repo/scheme.

    ``host`` is the bare lower-cased host name (no user info, no port): the identity used for provider
    detection and keyring lookups. ``port`` is the explicit web port ("" when none), and ``authority`` is
    ``host`` or ``host:port`` for building API URLs. SSH remotes (scp-like or ssh://) report scheme
    "https" and no port: an SSH port is not the web/API port.
    """
    value = str(url or "").strip()
    if not value:
        raise error_type("Empty git remote URL")

    # SCP-like syntax: [user@]host:owner/repo.git (no scheme, so no "://")
    if "://" not in value and "@" in value and ":" in value:
        left, path = value.split(":", 1)
        host = left.rpartition("@")[2].strip().lower()
        if not host:
            raise error_type(f"Unable to parse git remote URL: {redact_url(value)}")
        owner, repo = _owner_repo(path, value, error_type)
        return {"host": host, "port": "", "authority": host, "owner": owner, "repo": repo, "scheme": "https"}

    parsed = urlparse(value)
    try:
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:  # invalid port
        raise error_type(f"Unable to parse git remote URL: {redact_url(value)}") from exc
    if not host or not parsed.path:
        raise error_type(f"Unable to parse git remote URL: {redact_url(value)}")

    owner, repo = _owner_repo(parsed.path, value, error_type)
    scheme = (parsed.scheme or "https").lower()
    if scheme in _SSH_SCHEMES:
        scheme, port = "https", None
    port_text = str(port) if port is not None else ""
    authority = f"[{host}]" if ":" in host else host  # IPv6 literal
    if port_text:
        authority = f"{authority}:{port_text}"
    return {
        "host": host,
        "port": port_text,
        "authority": authority,
        "owner": owner,
        "repo": repo,
        "scheme": scheme,
    }


def detect_from_git(error_type: type[Exception] = RuntimeError) -> Dict[str, str]:
    """Detect provider/host/owner/repo from git remote.origin.url."""
    try:
        remote = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            text=True,
            timeout=10,
        ).strip()
    except Exception as exc:
        raise error_type(
            "Could not read git remote.origin.url; pass provider fields explicitly."
        ) from exc

    parsed = parse_git_remote(remote, error_type=error_type)
    host = parsed["host"]
    provider = "github" if host == "github.com" or host.endswith(".github.com") else "forgejo"

    parsed["provider"] = provider
    return parsed


def detect_provider_from_git(*, error_type: type[Exception] = RuntimeError) -> str:
    """Detect repository provider from git remote (github or forgejo)."""
    detected = detect_from_git(error_type=error_type)
    return str(detected["provider"])


def normalize_forgejo_api_base(base_url: str, error_type: type[Exception] = RuntimeError) -> str:
    """Normalize Forgejo URL into API base ending with /api/v1."""
    value = str(base_url or "").strip().rstrip("/")
    if not value:
        raise error_type("Forgejo URL is required")
    if value.endswith("/api/v1"):
        return value
    return f"{value}/api/v1"


def host_from_url(url_value: str, error_type: type[Exception] = RuntimeError) -> str:
    """Extract the bare lower-cased host name from a URL (no user info, no port)."""
    parsed = urlparse(str(url_value or "").strip())
    host = (parsed.hostname or "").lower()
    if not host:
        raise error_type(f"Unable to parse host from URL: {redact_url(url_value)}")
    return host


def authority_from_url(url_value: str, error_type: type[Exception] = RuntimeError) -> str:
    """Extract ``host`` or ``host:port`` from a URL, never the user info.

    Used as the keyring host for self-hosted servers so a server on a non-default port keeps the
    account names it always had (``host:port/owner``); resolve_token also tries the bare host.
    """
    parsed = urlparse(str(url_value or "").strip())
    host = host_from_url(url_value, error_type=error_type)
    try:
        port = parsed.port
    except ValueError as exc:
        raise error_type(f"Unable to parse port from URL: {redact_url(url_value)}") from exc
    authority = f"[{host}]" if ":" in host else host
    return f"{authority}:{port}" if port is not None else authority


def scoped_account(host: str, owner: str) -> str:
    """Build URI-like keyring account key: host/owner."""
    return f"{str(host or '').strip().lower()}/{str(owner or '').strip()}".strip("/")


def resolve_token(
    *,
    env_name: str,
    token_service: str,
    token_account: str,
    host: str,
    owner: str,
    legacy_account: str,
) -> Tuple[str, str, List[str]]:
    """Resolve token from env first, then keyring with scoped account fallback.

    Lookup order:
    1) env var
    2) explicit token_account (if provided)
    3) host/owner scoped account
    4) host-only account
    5) legacy provider account (github|forgejo)

    ``host`` may be ``host:port`` (see authority_from_url): the port-qualified accounts are tried
    first, then the same accounts with the bare host name. User info is never part of an account.
    """
    env_var = str(env_name or "").strip()
    token = str(os.getenv(env_var, "") or "").strip()
    if token:
        return token, f"env:{env_var}", []

    service = str(token_service or "").strip()
    explicit_account = str(token_account or "").strip()
    host_value = str(host or "").strip().lower().rpartition("@")[2]
    bare_host = (urlparse(f"//{host_value}").hostname or "") if host_value else ""
    owner_value = str(owner or "").strip()
    legacy = str(legacy_account or "").strip()

    accounts: List[str] = []
    if explicit_account:
        accounts.append(explicit_account)

    host_values = [h for h in dict.fromkeys((host_value, bare_host)) if h]
    for value in host_values:
        scoped = scoped_account(value, owner_value)
        if scoped and scoped not in accounts:
            accounts.append(scoped)

    for value in host_values:
        if value not in accounts:
            accounts.append(value)

    if legacy and legacy not in accounts:
        accounts.append(legacy)

    try:
        import keyring

        for account in accounts:
            candidate = str(keyring.get_password(service, account) or "").strip()
            if candidate:
                return candidate, f"keyring:{service}/{account}", accounts
    except Exception:
        pass

    checked = f"env:{env_var} or keyring:{service}/[{', '.join(accounts) or '<none>'}]"
    return "", checked, accounts
