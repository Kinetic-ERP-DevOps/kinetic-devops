"""RepoMaker-owned helper functions for smoke flow operations."""

from __future__ import annotations

import secrets
import string
from typing import Iterable, Optional, Tuple

from kinetic_devops import repo_context


class RepoMakerError(RuntimeError):
    """Base error for RepoMaker flow and provider context failures."""


def random_suffix(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def resolve_provider_owner(
    *,
    provider: str,
    owner: str,
    error_type: type[Exception],
) -> Tuple[str, str]:
    """Resolve owner and host for a provider, inferring from git remote when needed."""
    owner_value = str(owner or "").strip()

    if owner_value:
        return owner_value, ("github.com" if provider == "github" else "")

    detected = repo_context.detect_from_git(error_type=error_type)
    if detected["provider"] != provider:
        if provider == "github":
            raise error_type(
                "Current git remote does not appear to be GitHub. Use --provider forgejo or pass --owner explicitly."
            )
        raise error_type(
            "Current git remote appears to be GitHub. Use --provider github or pass Forgejo URL/owner explicitly."
        )

    return detected["owner"], detected["host"]


def resolve_provider_token(
    *,
    provider: str,
    env_name: str,
    token_service: str,
    token_account: str,
    host: str,
    owner: str,
) -> Tuple[str, str]:
    token, source, _ = repo_context.resolve_token(
        env_name=str(env_name or "").strip(),
        token_service=str(token_service or "kinetic-devops-tokens").strip() or "kinetic-devops-tokens",
        token_account=str(token_account or "").strip(),
        host=str(host or "").strip().lower(),
        owner=str(owner or "").strip(),
        legacy_account=provider,
    )
    return token, source


def apply_branch_protection_request(
    *,
    session,
    payload,
    timeout: int,
    create_endpoint: str,
    error_type: type[Exception],
    create_error_message: str,
    create_method: str = "put",
    fallback_update_endpoint: str = "",
    fallback_statuses: Optional[set[int]] = None,
    update_error_message: str = "",
) -> None:
    """Apply branch protection with optional fallback update endpoint."""
    method = str(create_method or "put").strip().lower()
    if method == "post":
        response = session.post(create_endpoint, json=payload, timeout=timeout)
    elif method == "put":
        response = session.put(create_endpoint, json=payload, timeout=timeout)
    else:
        raise error_type(f"Unsupported branch-protection method: {create_method}")

    if response.status_code // 100 == 2:
        return

    fallback = fallback_statuses or set()
    if fallback_update_endpoint and response.status_code in fallback:
        update = session.put(fallback_update_endpoint, json=payload, timeout=timeout)
        if update.status_code // 100 == 2:
            return
        raise error_type(update_error_message.format(status=update.status_code, body=update.text))

    raise error_type(create_error_message.format(status=response.status_code, body=response.text))


def create_repo_request(
    *,
    session,
    endpoint: str,
    payload,
    timeout: int,
    error_type: type[Exception],
    error_message: str,
) -> None:
    response = session.post(endpoint, json=payload, timeout=timeout)
    if response.status_code // 100 == 2:
        return
    raise error_type(error_message.format(status=response.status_code, body=response.text))


def fetch_json_request(
    *,
    session,
    endpoint: str,
    timeout: int,
    error_type: type[Exception],
    error_message: str,
):
    response = session.get(endpoint, timeout=timeout)
    if response.status_code // 100 == 2:
        return response.json()
    raise error_type(error_message.format(status=response.status_code, body=response.text))


def delete_repo_request(
    *,
    session,
    endpoint: str,
    timeout: int,
    error_type: type[Exception],
    error_message: str,
) -> None:
    response = session.delete(endpoint, timeout=timeout)
    if response.status_code // 100 == 2:
        return
    raise error_type(error_message.format(status=response.status_code, body=response.text))


def verify_required_controls(
    *,
    required_check: str,
    required_approvals: int,
    contexts: Iterable[str],
    approvals: int,
    error_type: type[Exception],
) -> None:
    context_values = list(contexts or [])
    expected_approvals = max(0, int(required_approvals))
    actual_approvals = int(approvals)

    if required_check not in context_values:
        raise error_type(
            f"Expected required check '{required_check}' not found in {context_values}"
        )

    if actual_approvals != expected_approvals:
        raise error_type(
            f"Expected required approvals {expected_approvals}, got {actual_approvals}"
        )
