"""RepoMaker apply engine for branch protection automation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from kinetic_devops import repo_context


DEFAULT_TOKEN_SERVICE = "kinetic-devops-tokens"


@dataclass
class Target:
    provider: str
    owner: str
    repo: str
    branch: str
    token_env: str
    required_checks: List[str]
    required_approvals: int
    enforce_admins: bool
    require_conversation_resolution: bool
    forgejo_api_base: str
    token_service: str = DEFAULT_TOKEN_SERVICE
    token_account: str = ""


class BranchProtectionError(RuntimeError):
    """Raised when a branch protection API call fails."""


def _parse_git_remote(url: str) -> Dict[str, str]:
    """Parse origin remote URL into host/owner/repo components."""
    value = str(url or "").strip()
    if not value:
        raise BranchProtectionError("Unable to parse empty git remote URL")

    # SCP-like syntax: git@host:owner/repo.git
    if "@" in value and ":" in value and not value.startswith(("http://", "https://", "ssh://")):
        _, right = value.split("@", 1)
        host, path = right.split(":", 1)
        owner_repo = path.rstrip("/")
        if owner_repo.endswith(".git"):
            owner_repo = owner_repo[:-4]
        parts = [p for p in owner_repo.split("/") if p]
        if len(parts) < 2:
            raise BranchProtectionError(f"Unable to parse owner/repo from git remote: {value}")
        return {"host": host.lower(), "owner": parts[-2], "repo": parts[-1], "scheme": "https"}

    parsed = urlparse(value)
    if not parsed.netloc or not parsed.path:
        raise BranchProtectionError(f"Unable to parse git remote URL: {value}")

    owner_repo = parsed.path.rstrip("/")
    if owner_repo.endswith(".git"):
        owner_repo = owner_repo[:-4]
    parts = [p for p in owner_repo.split("/") if p]
    if len(parts) < 2:
        raise BranchProtectionError(f"Unable to parse owner/repo from git remote: {value}")
    return {
        "host": parsed.netloc.lower(),
        "owner": parts[-2],
        "repo": parts[-1],
        "scheme": parsed.scheme or "https",
    }


def _detect_current_repo_from_git() -> Dict[str, str]:
    """Detect repository coordinates from git remote origin URL."""
    try:
        remote = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            text=True,
            timeout=10,
        ).strip()
    except Exception as exc:  # pragma: no cover - exercised via caller assertions.
        raise BranchProtectionError(
            "Could not read git remote.origin.url; run inside a git repo or specify owner/repo explicitly"
        ) from exc

    parsed = _parse_git_remote(remote)
    host = parsed["host"]
    provider = "github" if host == "github.com" or host.endswith(".github.com") else "forgejo"

    result = {
        "provider": provider,
        "owner": parsed["owner"],
        "repo": parsed["repo"],
    }
    if provider == "forgejo":
        scheme = parsed.get("scheme") or "https"
        result["forgejo_api_base"] = f"{scheme}://{host}/api/v1"

    return result


def _with_git_defaults(target: Target) -> Target:
    """Fill missing target fields from current git repository metadata."""
    needs_git_defaults = not target.owner or not target.repo or not target.provider
    needs_forgejo_api = target.provider == "forgejo" and not target.forgejo_api_base
    if not needs_git_defaults and not needs_forgejo_api:
        return target

    detected = _detect_current_repo_from_git()

    provider = target.provider or detected.get("provider", "")
    owner = target.owner or detected.get("owner", "")
    repo = target.repo or detected.get("repo", "")

    forgejo_api_base = target.forgejo_api_base
    if provider == "forgejo" and not forgejo_api_base:
        forgejo_api_base = str(detected.get("forgejo_api_base", "")).strip()

    return Target(
        provider=provider,
        owner=owner,
        repo=repo,
        branch=target.branch,
        token_env=target.token_env,
        token_service=target.token_service,
        token_account=target.token_account,
        required_checks=target.required_checks,
        required_approvals=target.required_approvals,
        enforce_admins=target.enforce_admins,
        require_conversation_resolution=target.require_conversation_resolution,
        forgejo_api_base=forgejo_api_base,
    )


def _load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise BranchProtectionError("Config root must be a JSON object")
    return data


def _merge_target(defaults: Dict[str, Any], raw_target: Dict[str, Any]) -> Target:
    merged = dict(defaults)
    merged.update(raw_target)

    provider = str(merged.get("provider", "")).strip().lower()
    if provider and provider not in {"github", "forgejo"}:
        raise BranchProtectionError(f"Unsupported provider: {provider}")

    required_checks_raw = merged.get("required_checks") or []
    if not isinstance(required_checks_raw, list):
        raise BranchProtectionError("required_checks must be an array")

    return Target(
        provider=provider,
        owner=str(merged.get("owner", "")).strip(),
        repo=str(merged.get("repo", "")).strip(),
        branch=str(merged.get("branch", "main")).strip(),
        token_env=str(merged.get("token_env", "")).strip(),
        token_service=str(merged.get("token_service", DEFAULT_TOKEN_SERVICE)).strip() or DEFAULT_TOKEN_SERVICE,
        token_account=str(merged.get("token_account", "")).strip(),
        required_checks=[str(item).strip() for item in required_checks_raw if str(item).strip()],
        required_approvals=int(merged.get("required_approvals", 1)),
        enforce_admins=bool(merged.get("enforce_admins", True)),
        require_conversation_resolution=bool(merged.get("require_conversation_resolution", True)),
        forgejo_api_base=str(merged.get("forgejo_api_base", "")).strip(),
    )


def _parse_targets(config: Dict[str, Any]) -> List[Target]:
    defaults = config.get("defaults") or {}
    raw_targets = config.get("targets") or []

    if not isinstance(defaults, dict):
        raise BranchProtectionError("defaults must be an object")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise BranchProtectionError("targets must be a non-empty array")

    parsed: List[Target] = []
    for index, item in enumerate(raw_targets, start=1):
        if not isinstance(item, dict):
            raise BranchProtectionError(f"targets[{index}] must be an object")
        target = _merge_target(defaults, item)
        if not target.token_env:
            raise BranchProtectionError(f"targets[{index}] missing token_env")
        parsed.append(target)
    return parsed


def _resolve_token(target: Target) -> tuple[str, str, str, str]:
    """Resolve provider token from env var first, then keyring fallback."""
    env_name = target.token_env
    service = target.token_service or DEFAULT_TOKEN_SERVICE

    if target.provider == "github":
        host = "github.com"
    else:
        host = repo_context.host_from_url(target.forgejo_api_base, error_type=BranchProtectionError)

    token, source, accounts = repo_context.resolve_token(
        env_name=env_name,
        token_service=service,
        token_account=target.token_account,
        host=host,
        owner=target.owner,
        legacy_account=target.provider,
    )

    if source.startswith("env:"):
        return token, "env", env_name, ""

    if source.startswith("keyring:"):
        _, right = source.split(":", 1)
        svc, account = right.split("/", 1)
        return token, "keyring", svc, account

    checked_accounts = ", ".join(accounts)
    return "", "", service, checked_accounts


def _github_payload(target: Target) -> Dict[str, Any]:
    required_status_checks: Optional[Dict[str, Any]] = None
    if target.required_checks:
        required_status_checks = {
            "strict": True,
            "contexts": target.required_checks,
        }

    return {
        "required_status_checks": required_status_checks,
        "enforce_admins": target.enforce_admins,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "required_approving_review_count": max(0, target.required_approvals),
            "require_last_push_approval": False,
        },
        "restrictions": None,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "block_creations": False,
        "required_conversation_resolution": target.require_conversation_resolution,
        "lock_branch": False,
        "allow_fork_syncing": False,
    }


def _forgejo_payload(target: Target) -> Dict[str, Any]:
    # Forgejo follows Gitea branch protection fields.
    return {
        "branch_name": target.branch,
        "enable_push": False,
        "enable_push_whitelist": False,
        "enable_merge_whitelist": False,
        "enable_status_check": bool(target.required_checks),
        "status_check_contexts": target.required_checks,
        "required_approvals": max(0, target.required_approvals),
        "block_on_official_review_requests": True,
        "block_on_outdated_branch": True,
        "block_on_rejected_reviews": True,
        "dismiss_stale_approvals": True,
    }


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _apply_github(target: Target, token: str, dry_run: bool) -> None:
    endpoint = f"https://api.github.com/repos/{target.owner}/{target.repo}/branches/{target.branch}/protection"
    payload = _github_payload(target)

    print(f"[github] {target.owner}/{target.repo}:{target.branch}")
    print(f"  endpoint: {endpoint}")
    print(f"  required_checks: {target.required_checks or ['<none>']}")
    print(f"  required_approvals: {target.required_approvals}")
    if dry_run:
        print("  mode: dry-run (no API call)")
        return

    response = requests.put(endpoint, headers=_headers(token), json=payload, timeout=60)
    if response.status_code // 100 != 2:
        raise BranchProtectionError(
            f"GitHub protection apply failed ({response.status_code}): {response.text}"
        )


def _apply_forgejo(target: Target, token: str, dry_run: bool) -> None:
    api_base = target.forgejo_api_base.rstrip("/")
    collection_endpoint = f"{api_base}/repos/{target.owner}/{target.repo}/branch_protections"
    single_endpoint = f"{collection_endpoint}/{target.branch}"
    payload = _forgejo_payload(target)

    print(f"[forgejo] {target.owner}/{target.repo}:{target.branch}")
    print(f"  endpoint: {collection_endpoint}")
    print(f"  required_checks: {target.required_checks or ['<none>']}")
    print(f"  required_approvals: {target.required_approvals}")
    if dry_run:
        print("  mode: dry-run (no API call)")
        return

    headers = _headers(token)
    create_response = requests.post(collection_endpoint, headers=headers, json=payload, timeout=60)

    if create_response.status_code // 100 == 2:
        return

    # Existing rule or provider differences: update by branch key.
    if create_response.status_code in {409, 422, 404, 405}:
        update_response = requests.put(single_endpoint, headers=headers, json=payload, timeout=60)
        if update_response.status_code // 100 == 2:
            return
        raise BranchProtectionError(
            "Forgejo protection update failed "
            f"({update_response.status_code}): {update_response.text}"
        )

    raise BranchProtectionError(
        f"Forgejo protection create failed ({create_response.status_code}): {create_response.text}"
    )


def _run_target(target: Target, dry_run: bool) -> None:
    target = _with_git_defaults(target)

    if target.provider not in {"github", "forgejo"}:
        raise BranchProtectionError(
            "Unable to determine provider. Set target provider or ensure git remote host is github.com or Forgejo/Gitea."
        )

    if not target.owner or not target.repo:
        raise BranchProtectionError("Unable to determine owner/repo. Set them explicitly or run inside a git repo.")

    if target.provider == "forgejo" and not target.forgejo_api_base and not dry_run:
        raise BranchProtectionError(
            "Missing forgejo_api_base. Set it explicitly or run inside a git repo with Forgejo remote origin."
        )

    token = ""
    source = ""
    source_name = ""
    source_account = ""
    if not dry_run:
        token, source, source_name, source_account = _resolve_token(target)

        if source == "env":
            print(f"  token_source: env:{source_name}")
        elif source == "keyring":
            print(f"  token_source: keyring:{source_name}/{source_account}")

        if not token:
            raise BranchProtectionError(
                "Token not found. "
                f"Set env var '{target.token_env}' or keyring '{source_name}' account '{source_account}'."
            )

    if target.provider == "github":
        _apply_github(target, token=token, dry_run=dry_run)
        return

    if target.provider == "forgejo":
        _apply_forgejo(target, token=token, dry_run=dry_run)
        return

    raise BranchProtectionError(f"Unsupported provider: {target.provider}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply branch protection rules for GitHub and Forgejo targets. Reads the target repos/branches "
            "and rules (required checks, approvals, ...) from a config JSON and, with --apply, PUTs/POSTs "
            "the protection settings to each provider's API, creating or overwriting the rule for that branch. "
            "Without --apply it is a dry-run."
        ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default="scripts/branch_protection.targets.example.json",
        help="Path to branch protection target config JSON (default: %(default)s)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this flag, script runs in dry-run mode.",
    )

    args = parser.parse_args(argv)
    dry_run = not args.apply

    try:
        config_path = Path(args.config)
        config = _load_config(config_path)
        targets = _parse_targets(config)

        print("=" * 72)
        print("Branch Protection Automation")
        print("=" * 72)
        print(f"Config: {config_path}")
        print(f"Mode: {'apply' if not dry_run else 'dry-run'}")
        print(f"Targets: {len(targets)}")
        print("")

        for target in targets:
            _run_target(target, dry_run=dry_run)
            print("")

        print("Completed successfully.")
        return 0
    except BranchProtectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"ERROR: config file not found: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"ERROR: invalid JSON config: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
