"""RepoMaker unified smoke flow for GitHub and Forgejo branch protection."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import List, Optional

from kinetic_devops import _lazy_import

requests = _lazy_import("requests")

from kinetic_devops import repo_context
from kinetic_devops import repo_maker_core


RepoMakerError = repo_maker_core.RepoMakerError


@dataclass
class SmokeConfig:
    provider: str
    api_base: str
    owner: str
    owner_type: str
    repo_name: str
    branch: str
    required_check: str
    required_approvals: int
    token: str
    keep_repo: bool
    timeout: int


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run RepoMaker GitHub/Forgejo branch-protection smoke flow.\n"
            "Dry-run by default (prints the plan, no API calls). With --apply it creates a private temporary\n"
            "repository, applies branch protection (required status check + PR approvals), verifies it, and\n"
            "then DELETES the repository it created unless --keep-repo is given."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.repo_maker --provider github --owner my-org             # dry-run\n"
            "  python -m kinetic_devops.repo_maker --provider github --owner my-org --apply"
        ),
    )
    parser.add_argument(
        "--provider", choices=("auto", "github", "forgejo"), default="auto",
        help="Git hosting provider; auto detects it from the current git remote (default: auto)",
    )
    parser.add_argument(
        "--owner", metavar="OWNER", default="",
        help="Repository owner/user/org (default: GITHUB_OWNER / FORGEJO_OWNER, or the current git remote for Forgejo)",
    )
    parser.add_argument(
        "--owner-type", choices=("org", "user"), default="org",
        help="Create the repo under an organization or the token's own user account (default: org)",
    )
    parser.add_argument("--repo", metavar="NAME", default="", help="Repo name (default: generated temporary name bp-smoke-<random>)")
    parser.add_argument("--branch", metavar="BRANCH", default="main", help="Target branch to protect (default: main)")
    parser.add_argument(
        "--required-check", metavar="CONTEXT", default="Python Test Gate",
        help="Required status-check context (default: %(default)s)",
    )
    parser.add_argument("--required-approvals", metavar="N", type=int, default=1, help="Required PR approvals (default: 1)")
    parser.add_argument(
        "--token-env", metavar="VAR", default="",
        help="Optional token env-var override (default: GITHUB_TOKEN or FORGEJO_TOKEN)",
    )
    parser.add_argument(
        "--token-service", metavar="SERVICE", default="kinetic-devops-tokens",
        help="Keyring service name to read the API token from (default: %(default)s)",
    )
    parser.add_argument("--token-account", metavar="ACCOUNT", default="", help="Keyring account override")
    parser.add_argument("--timeout", metavar="SECONDS", type=int, default=60, help="HTTP timeout in seconds (default: 60)")
    parser.add_argument("--keep-repo", action="store_true", help="Keep temporary repository instead of deleting it")
    parser.add_argument(
        "--apply", action="store_true",
        help="Execute API calls (creates, protects and normally deletes a repo); without it, dry-run only",
    )
    parser.add_argument(
        "--forgejo-url", metavar="URL", default="",
        help="Forgejo base URL for forgejo provider (default: derived from the current git remote)",
    )
    return parser.parse_args(argv)


def github_api_base(host: str) -> str:
    host_value = str(host or "").strip().lower()
    if not host_value or host_value == "github.com":
        return "https://api.github.com"
    return f"https://{host_value}/api/v3"


def build_branch_protection_payload(
    provider: str,
    branch: str,
    required_check: str,
    required_approvals: int,
):
    if provider == "github":
        return {
            "required_status_checks": {
                "strict": True,
                "contexts": [required_check],
            },
            "enforce_admins": True,
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": True,
                "require_code_owner_reviews": False,
                "required_approving_review_count": max(0, int(required_approvals)),
                "require_last_push_approval": False,
            },
            "restrictions": None,
            "required_linear_history": False,
            "allow_force_pushes": False,
            "allow_deletions": False,
            "block_creations": False,
            "required_conversation_resolution": True,
            "lock_branch": False,
            "allow_fork_syncing": False,
        }

    if provider == "forgejo":
        return {
            "branch_name": branch,
            "enable_push": False,
            "enable_push_whitelist": False,
            "enable_merge_whitelist": False,
            "enable_status_check": True,
            "status_check_contexts": [required_check],
            "required_approvals": max(0, int(required_approvals)),
            "block_on_official_review_requests": True,
            "block_on_outdated_branch": True,
            "block_on_rejected_reviews": True,
            "dismiss_stale_approvals": True,
        }

    raise RepoMakerError(f"Unsupported provider: {provider}")


def _headers(provider: str, token: str):
    if provider == "github":
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }
    return {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _resolve_context(provider: str, args: argparse.Namespace):
    owner_value = str(args.owner or "").strip()
    if not owner_value:
        owner_env = "GITHUB_OWNER" if provider == "github" else "FORGEJO_OWNER"
        owner_value = str(os.getenv(owner_env, "")).strip()

    if provider == "github":
        owner, host = repo_maker_core.resolve_provider_owner(
            provider="github",
            owner=owner_value,
            error_type=RepoMakerError,
        )
        if not host:
            host = "github.com"
        return owner, host, github_api_base(host)

    url_value = str(args.forgejo_url or "").strip()
    owner = owner_value
    host = ""

    if not url_value or not owner:
        detected = repo_context.detect_from_git(error_type=RepoMakerError)
        if detected["provider"] != "forgejo":
            raise RepoMakerError(
                "Current git remote appears to be GitHub. Use --provider github or pass Forgejo URL/owner explicitly."
            )
        if not url_value:
            url_value = f"{detected.get('scheme', 'https')}://{detected['host']}/api/v1"
        if not owner:
            owner = detected["owner"]
        host = detected["host"]

    api_base = repo_context.normalize_forgejo_api_base(url_value, error_type=RepoMakerError)
    if not host:
        host = repo_context.host_from_url(api_base, error_type=RepoMakerError)
    return owner, host, api_base


def _create_repo(session: requests.Session, cfg: SmokeConfig):
    payload = {
        "name": cfg.repo_name,
        "description": "Temporary branch protection smoke-test repository",
        "private": True,
        "auto_init": True,
    }
    if cfg.provider == "forgejo":
        payload["default_branch"] = cfg.branch

    if cfg.owner_type == "org":
        endpoint = f"{cfg.api_base}/orgs/{cfg.owner}/repos"
    else:
        endpoint = f"{cfg.api_base}/user/repos"

    repo_maker_core.create_repo_request(
        session=session,
        endpoint=endpoint,
        payload=payload,
        timeout=cfg.timeout,
        error_type=RepoMakerError,
        error_message=f"{cfg.provider} repo creation failed ({{status}}): {{body}}",
    )


def _apply_branch_protection(session: requests.Session, cfg: SmokeConfig):
    payload = build_branch_protection_payload(
        cfg.provider,
        cfg.branch,
        cfg.required_check,
        cfg.required_approvals,
    )

    if cfg.provider == "github":
        endpoint = f"{cfg.api_base}/repos/{cfg.owner}/{cfg.repo_name}/branches/{cfg.branch}/protection"
        repo_maker_core.apply_branch_protection_request(
            session=session,
            payload=payload,
            timeout=cfg.timeout,
            create_endpoint=endpoint,
            create_method="put",
            error_type=RepoMakerError,
            create_error_message="github branch protection apply failed ({status}): {body}",
        )
        return

    collection = f"{cfg.api_base}/repos/{cfg.owner}/{cfg.repo_name}/branch_protections"
    single = f"{collection}/{cfg.branch}"
    repo_maker_core.apply_branch_protection_request(
        session=session,
        payload=payload,
        timeout=cfg.timeout,
        create_endpoint=collection,
        create_method="post",
        fallback_update_endpoint=single,
        fallback_statuses={404, 405, 409, 422},
        error_type=RepoMakerError,
        create_error_message="forgejo branch protection create failed ({status}): {body}",
        update_error_message="forgejo branch protection update failed ({status}): {body}",
    )


def _verify_branch_protection(session: requests.Session, cfg: SmokeConfig):
    if cfg.provider == "github":
        endpoint = f"{cfg.api_base}/repos/{cfg.owner}/{cfg.repo_name}/branches/{cfg.branch}/protection"
        body = repo_maker_core.fetch_json_request(
            session=session,
            endpoint=endpoint,
            timeout=cfg.timeout,
            error_type=RepoMakerError,
            error_message="github branch protection fetch failed ({status}): {body}",
        )
        contexts = (body.get("required_status_checks") or {}).get("contexts") or []
        approvals = int(
            ((body.get("required_pull_request_reviews") or {}).get("required_approving_review_count") or 0)
        )
    else:
        endpoint = f"{cfg.api_base}/repos/{cfg.owner}/{cfg.repo_name}/branch_protections/{cfg.branch}"
        body = repo_maker_core.fetch_json_request(
            session=session,
            endpoint=endpoint,
            timeout=cfg.timeout,
            error_type=RepoMakerError,
            error_message="forgejo branch protection fetch failed ({status}): {body}",
        )
        contexts = body.get("status_check_contexts") or []
        approvals = int(body.get("required_approvals", 0))

    repo_maker_core.verify_required_controls(
        required_check=cfg.required_check,
        required_approvals=cfg.required_approvals,
        contexts=contexts,
        approvals=approvals,
        error_type=RepoMakerError,
    )


def _delete_repo(session: requests.Session, cfg: SmokeConfig):
    endpoint = f"{cfg.api_base}/repos/{cfg.owner}/{cfg.repo_name}"
    repo_maker_core.delete_repo_request(
        session=session,
        endpoint=endpoint,
        timeout=cfg.timeout,
        error_type=RepoMakerError,
        error_message=f"{cfg.provider} repo delete failed ({{status}}): {{body}}",
    )


def run_smoke(cfg: SmokeConfig, dry_run: bool) -> None:
    print("=" * 72)
    print(f"RepoMaker {cfg.provider.capitalize()} Smoke")
    print("=" * 72)
    print(f"API base: {cfg.api_base}")
    print(f"Owner: {cfg.owner} ({cfg.owner_type})")
    print(f"Repo: {cfg.repo_name}")
    print(f"Branch: {cfg.branch}")
    print(f"Required check: {cfg.required_check}")
    print(f"Required approvals: {cfg.required_approvals}")
    print(f"Cleanup: {'delete repo after test' if not cfg.keep_repo else 'keep repo'}")

    if dry_run:
        print("Mode: dry-run (no API calls)")
        print("Steps:")
        print("1. Create repository")
        print("2. Apply branch protection")
        print("3. Verify branch protection")
        print("4. Delete repository (unless --keep-repo)")
        return

    session = requests.Session()
    session.headers.update(_headers(cfg.provider, cfg.token))

    created = False
    try:
        _create_repo(session, cfg)
        created = True
        print("[ok] repository created")

        _apply_branch_protection(session, cfg)
        print("[ok] branch protection applied")

        _verify_branch_protection(session, cfg)
        print("[ok] branch protection verified")
    finally:
        if created and not cfg.keep_repo:
            try:
                _delete_repo(session, cfg)
                print("[ok] repository deleted")
            except Exception as exc:  # pragma: no cover
                print(f"[warn] cleanup failed: {exc}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        provider = args.provider
        if provider == "auto":
            provider = repo_context.detect_provider_from_git(error_type=RepoMakerError)
        if provider not in {"github", "forgejo"}:
            raise RepoMakerError(f"Unsupported provider: {provider}")

        owner, host, api_base = _resolve_context(provider, args)
        dry_run = not args.apply
        token_env_default = "GITHUB_TOKEN" if provider == "github" else "FORGEJO_TOKEN"

        token, token_source = repo_maker_core.resolve_provider_token(
            provider=provider,
            env_name=args.token_env or token_env_default,
            token_service=args.token_service,
            token_account=args.token_account,
            host=host,
            owner=owner,
        )
        if not token and not dry_run:
            raise RepoMakerError(f"Missing {provider} token. Checked {token_source}.")

        cfg = SmokeConfig(
            provider=provider,
            api_base=api_base,
            owner=owner,
            owner_type=args.owner_type,
            repo_name=str(args.repo or "").strip() or f"bp-smoke-{repo_maker_core.random_suffix()}",
            branch=args.branch,
            required_check=args.required_check,
            required_approvals=args.required_approvals,
            token=token,
            keep_repo=bool(args.keep_repo),
            timeout=max(1, int(args.timeout)),
        )

        run_smoke(cfg, dry_run=dry_run)
        print("SMOKE RESULT: PASS")
        return 0
    except RepoMakerError as exc:
        print(f"SMOKE RESULT: FAIL - {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
