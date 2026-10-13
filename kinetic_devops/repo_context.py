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


def parse_git_remote(url: str, error_type: type[Exception] = RuntimeError) -> Dict[str, str]:
    """Parse git remote URL and extract host/owner/repo/scheme."""
    value = str(url or "").strip()
    if not value:
        raise error_type("Empty git remote URL")

    # SCP-like syntax: git@host:owner/repo.git
    if "@" in value and ":" in value and not value.startswith(("http://", "https://", "ssh://")):
        _, right = value.split("@", 1)
        host, path = right.split(":", 1)
        owner_repo = path.rstrip("/")
        if owner_repo.endswith(".git"):
            owner_repo = owner_repo[:-4]
        parts = [p for p in owner_repo.split("/") if p]
        if len(parts) < 2:
            raise error_type(f"Unable to parse owner/repo from git remote: {value}")
        return {
            "host": host.lower(),
            "owner": parts[-2],
            "repo": parts[-1],
            "scheme": "https",
        }

    parsed = urlparse(value)
    if not parsed.netloc or not parsed.path:
        raise error_type(f"Unable to parse git remote URL: {value}")

    owner_repo = parsed.path.rstrip("/")
    if owner_repo.endswith(".git"):
        owner_repo = owner_repo[:-4]
    parts = [p for p in owner_repo.split("/") if p]
    if len(parts) < 2:
        raise error_type(f"Unable to parse owner/repo from git remote: {value}")
    return {
        "host": parsed.netloc.lower(),
        "owner": parts[-2],
        "repo": parts[-1],
        "scheme": parsed.scheme or "https",
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
    """Extract hostname/netloc from URL value."""
    parsed = urlparse(str(url_value or "").strip())
    if not parsed.netloc:
        raise error_type(f"Unable to parse host from URL: {url_value}")
    return parsed.netloc.lower()


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
    """
    env_var = str(env_name or "").strip()
    token = str(os.getenv(env_var, "") or "").strip()
    if token:
        return token, f"env:{env_var}", []

    service = str(token_service or "").strip()
    explicit_account = str(token_account or "").strip()
    host_value = str(host or "").strip().lower()
    owner_value = str(owner or "").strip()
    legacy = str(legacy_account or "").strip()

    accounts: List[str] = []
    if explicit_account:
        accounts.append(explicit_account)

    scoped = scoped_account(host_value, owner_value)
    if scoped and scoped not in accounts:
        accounts.append(scoped)

    if host_value and host_value not in accounts:
        accounts.append(host_value)

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
