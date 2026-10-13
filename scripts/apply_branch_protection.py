#!/usr/bin/env python
"""Compatibility wrapper for RepoMaker apply engine.

Canonical implementation lives in kinetic_devops.repomaker.apply_engine.
"""

from __future__ import annotations

from kinetic_devops.repomaker import apply_engine as _impl

DEFAULT_TOKEN_SERVICE = _impl.DEFAULT_TOKEN_SERVICE
Target = _impl.Target
BranchProtectionError = _impl.BranchProtectionError

_parse_git_remote = _impl._parse_git_remote
_detect_current_repo_from_git = _impl._detect_current_repo_from_git
_load_config = _impl._load_config
_merge_target = _impl._merge_target
_parse_targets = _impl._parse_targets
_resolve_token = _impl._resolve_token
_github_payload = _impl._github_payload
_forgejo_payload = _impl._forgejo_payload
_headers = _impl._headers
_apply_github = _impl._apply_github
_apply_forgejo = _impl._apply_forgejo


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


def main(argv=None) -> int:
    return _impl.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
