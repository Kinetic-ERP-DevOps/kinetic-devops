"""Shared repository host context and token resolution helpers.

This module centralizes:
- Git remote parsing and provider detection
- Forgejo API base normalization
- Keyring token lookup strategy for multi-repo/multi-owner setups
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple
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
    """Return the URL without its user info (``user[:password]@``) or query/fragment, for messages and logs.

    The user info ends at the last ``@`` before the first ``?``/``#``, so a password containing ``/``,
    ``:`` or ``@`` is cut whole (splitting at the first ``/`` would leak it); removed user info shows as
    ``***@``. If an ``@`` remains after that (in a query/fragment, or a ``?``/``#`` inside the user info),
    the user info can't be told apart, so a fixed placeholder is returned instead of any part of the URL.
    """
    text = str(value or "")
    prefix, rest = "", text
    if "://" in text:
        scheme, rest = text.split("://", 1)
        prefix = f"{scheme}://"
    cut = min((i for i in (rest.find("?"), rest.find("#")) if i >= 0), default=len(rest))
    head, tail = rest[:cut], rest[cut:]
    at = head.rfind("@")
    if at >= 0:
        head = f"***@{head[at + 1:]}"
    if "@" in tail:
        return "<URL redacted>"
    return f"{prefix}{head}"


def _parse_error(value: str, error_type: type[Exception]) -> Exception:
    return error_type(f"Unable to parse git remote URL: {redact_url(value)}")


def _is_scp_like(value: str) -> bool:
    """Git's rule (connect.c, url_is_local_not_ssh): no "://", and a ":" before any "/".

    A Windows drive prefix (``C:\\`` or ``C:/``) is a local path, not host ``C``.
    """
    if "://" in value:
        return False
    colon, slash = value.find(":"), value.find("/")
    if colon < 0 or 0 <= slash < colon:
        return False
    if colon == 1 and value[0].isalpha() and value[2:3] in ("\\", "/"):
        return False
    return True


def _parse_scp_like(value: str, error_type: type[Exception]) -> Dict[str, str]:
    """Parse ``[user@]host:path``, ``[user@][host]:path`` or ``[user@host:port]:path`` (SSH; port ignored)."""
    colon = value.find(":")
    if value.startswith("["):
        open_at = 0
    else:
        marker = value.find("@[")
        open_at = marker + 1 if 0 <= marker < colon else -1
    if open_at >= 0:
        close = value.find("]", open_at)
        if close < 0 or value[close + 1:close + 2] != ":":
            raise _parse_error(value, error_type)
        host = value[open_at + 1:close].rpartition("@")[2]
        if host.count(":") == 1:  # [user@host:port]: an SSH port, not a web port
            host = host.split(":", 1)[0]
        path = value[close + 2:]
    else:
        host = value[:colon].rpartition("@")[2]
        path = value[colon + 1:]
    host = host.strip().lower()
    if not host or any(c in host for c in "[]@/\\ \t"):
        raise _parse_error(value, error_type)
    owner, repo = _owner_repo(path, value, error_type)
    authority = f"[{host}]" if ":" in host else host  # IPv6 literal
    return {"host": host, "port": "", "authority": authority, "owner": owner, "repo": repo, "scheme": "https"}


def parse_git_remote(url: str, error_type: type[Exception] = RuntimeError) -> Dict[str, str]:
    """Parse git remote URL and extract host/owner/repo/scheme.

    ``host`` is the bare lower-cased host name (no user info, no port): the identity used for provider
    detection and keyring lookups. ``port`` is the explicit web port ("" when none), and ``authority`` is
    ``host`` or ``host:port`` for building API URLs. SSH remotes (scp-like or ssh://) report scheme
    "https" and no port: an SSH port is not the web/API port. scp-like remotes follow git's rule (no
    "://" and a ":" before any "/"), so an ssh-config alias (``myalias:owner/repo``) is a host and
    ``./a@b:c/o/r`` is a local path (an error here). Errors never echo the URL's user info.
    """
    value = str(url or "").strip()
    if not value:
        raise error_type("Empty git remote URL")

    if _is_scp_like(value):
        return _parse_scp_like(value, error_type)

    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:  # invalid port or IPv6 literal; from None: its message can quote part of a password
        raise _parse_error(value, error_type) from None
    if not host or not parsed.path:
        raise _parse_error(value, error_type)

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


def _host_and_port(url_value: str, error_type: type[Exception]) -> Tuple[str, Optional[int]]:
    try:
        parsed = urlparse(str(url_value or "").strip())
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:  # invalid port or IPv6 literal; from None: its message can quote part of a password
        raise error_type(f"Unable to parse URL: {redact_url(url_value)}") from None
    if not host:
        raise error_type(f"Unable to parse host from URL: {redact_url(url_value)}")
    return host, port


def host_from_url(url_value: str, error_type: type[Exception] = RuntimeError) -> str:
    """Extract the bare lower-cased host name from a URL (no user info, no port)."""
    return _host_and_port(url_value, error_type)[0]


def authority_from_url(url_value: str, error_type: type[Exception] = RuntimeError) -> str:
    """Extract ``host`` or ``host:port`` from a URL, never the user info.

    Used as the keyring host for self-hosted servers so a server on a non-default port keeps the
    account names it always had (``host:port/owner``, ``host:port``).
    """
    host, port = _host_and_port(url_value, error_type)
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

    ``host`` may be ``host:port`` (see authority_from_url). Then only the port-qualified accounts are
    tried (``host:port/owner``, ``host:port``), never the bare host's: those belong to the server on the
    default port, and its token must not be sent to another server. User info is never part of an
    account.
    """
    env_var = str(env_name or "").strip()
    token = str(os.getenv(env_var, "") or "").strip()
    if token:
        return token, f"env:{env_var}", []

    service = str(token_service or "").strip()
    explicit_account = str(token_account or "").strip()
    host_value = str(host or "").strip().lower().rpartition("@")[2]
    owner_value = str(owner or "").strip()
    legacy = str(legacy_account or "").strip()

    accounts: List[str] = []
    if explicit_account:
        accounts.append(explicit_account)

    if host_value:
        for account in (scoped_account(host_value, owner_value), host_value):
            if account and account not in accounts:
                accounts.append(account)

    if legacy and legacy not in accounts:
        accounts.append(legacy)

    try:
        import keyring

        for account in accounts:
            candidate = str(keyring.get_password(service, account) or "").strip()
            if candidate:
                if account == legacy and host_value and account != explicit_account and not (
                        legacy == "github" and host_value == "github.com"):   # github.com: the account's own host
                    # The provider-wide legacy account isn't tied to a server: say which token is used for this
                    # host, so a token meant for another server doesn't go there unnoticed.
                    print(f"WARNING: no token stored for {scoped_account(host_value, owner_value) or host_value}; "
                          f"using the legacy keyring account {service}/{legacy} for {host_value}. Store one for "
                          f"this server (account {host_value}) if that token belongs to another.", file=sys.stderr)
                return candidate, f"keyring:{service}/{account}", accounts
    except Exception:
        pass

    checked = f"env:{env_var} or keyring:{service}/[{', '.join(accounts) or '<none>'}]"
    return "", checked, accounts
