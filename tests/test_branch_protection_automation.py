"""Regression tests for scripts/apply_branch_protection.py."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


class TestBranchProtectionAutomation(unittest.TestCase):
    """Validate branch protection automation script behavior."""

    @classmethod
    def setUpClass(cls):
        repo_root = Path(__file__).resolve().parents[1]
        script_path = repo_root / "scripts" / "apply_branch_protection.py"

        spec = importlib.util.spec_from_file_location("apply_branch_protection", script_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Unable to load apply_branch_protection.py")

        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.mod = module

    def test_parse_targets_from_example_config(self):
        config_path = Path("scripts/branch_protection.targets.example.json")
        config = self.mod._load_config(config_path)
        targets = self.mod._parse_targets(config)

        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0].provider, "github")
        self.assertEqual(targets[1].provider, "forgejo")
        self.assertEqual(
            targets[0].required_checks,
            [
                "Python Test Gate (required, py3.10)",
                "Python Test Gate (required, py3.12)",
            ],
        )

    def test_payloads_include_expected_controls(self):
        target = self.mod.Target(
            provider="github",
            owner="example",
            repo="repo",
            branch="main",
            token_env="GITHUB_TOKEN",
            required_checks=["Python Test Gate"],
            required_approvals=1,
            enforce_admins=True,
            require_conversation_resolution=True,
            forgejo_api_base="",
        )

        gh_payload = self.mod._github_payload(target)
        self.assertTrue(gh_payload["required_status_checks"]["strict"])
        self.assertEqual(gh_payload["required_pull_request_reviews"]["required_approving_review_count"], 1)

        forgejo_target = self.mod.Target(
            provider="forgejo",
            owner="example",
            repo="repo",
            branch="main",
            token_env="FORGEJO_TOKEN",
            required_checks=["Python Test Gate"],
            required_approvals=1,
            enforce_admins=True,
            require_conversation_resolution=True,
            forgejo_api_base="https://forgejo.example.com/api/v1",
        )
        fj_payload = self.mod._forgejo_payload(forgejo_target)
        self.assertTrue(fj_payload["enable_status_check"])
        self.assertEqual(fj_payload["status_check_contexts"], ["Python Test Gate"])

    def test_main_dry_run_returns_zero(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = self.mod.main([
                "--config",
                "scripts/branch_protection.targets.example.json",
            ])

        self.assertEqual(code, 0)
        self.assertIn("Mode: dry-run", stdout.getvalue())

    def test_main_apply_without_token_fails(self):
        cfg = {
            "defaults": {
                "branch": "main",
                "required_checks": ["Python Test Gate"],
                "required_approvals": 1,
                "enforce_admins": True,
                "require_conversation_resolution": True,
            },
            "targets": [
                {
                    "provider": "github",
                    "owner": "example",
                    "repo": "repo",
                    "token_env": "MISSING_TEST_TOKEN",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "targets.json"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

            # Ensure missing token condition for this test.
            if "MISSING_TEST_TOKEN" in os.environ:
                del os.environ["MISSING_TEST_TOKEN"]

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch("keyring.get_password", return_value=None):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = self.mod.main([
                        "--config",
                        str(cfg_path),
                        "--apply",
                    ])

        self.assertEqual(code, 1)
        self.assertIn("Token not found", stderr.getvalue())

    def test_run_target_uses_keyring_when_env_missing(self):
        target = self.mod.Target(
            provider="github",
            owner="example",
            repo="repo",
            branch="main",
            token_env="GITHUB_TOKEN",
            token_service="kinetic-devops-tokens",
            token_account="github",
            required_checks=["Python Test Gate"],
            required_approvals=1,
            enforce_admins=True,
            require_conversation_resolution=True,
            forgejo_api_base="",
        )

        with patch.dict("os.environ", {}, clear=True):
            with patch("keyring.get_password", return_value="kr_token"):
                with patch.object(self.mod, "_apply_github") as apply_gh:
                    self.mod._run_target(target, dry_run=False)

        apply_gh.assert_called_once()
        self.assertEqual(apply_gh.call_args.kwargs["token"], "kr_token")

    def test_with_git_defaults_infers_forgejo_target(self):
        target = self.mod.Target(
            provider="",
            owner="",
            repo="",
            branch="main",
            token_env="FORGEJO_TOKEN",
            required_checks=["Python Test Gate"],
            required_approvals=1,
            enforce_admins=True,
            require_conversation_resolution=True,
            forgejo_api_base="",
        )

        with patch.object(
            self.mod,
            "_detect_current_repo_from_git",
            return_value={
                "provider": "forgejo",
                "owner": "acme",
                "repo": "platform",
                "forgejo_api_base": "https://forgejo.acme.local/api/v1",
            },
        ):
            resolved = self.mod._with_git_defaults(target)

        self.assertEqual(resolved.provider, "forgejo")
        self.assertEqual(resolved.owner, "acme")
        self.assertEqual(resolved.repo, "platform")
        self.assertEqual(resolved.forgejo_api_base, "https://forgejo.acme.local/api/v1")

    def test_with_git_defaults_preserves_explicit_values(self):
        target = self.mod.Target(
            provider="github",
            owner="explicit-owner",
            repo="explicit-repo",
            branch="main",
            token_env="GITHUB_TOKEN",
            required_checks=["Python Test Gate"],
            required_approvals=1,
            enforce_admins=True,
            require_conversation_resolution=True,
            forgejo_api_base="",
        )

        with patch.object(
            self.mod,
            "_detect_current_repo_from_git",
            return_value={
                "provider": "forgejo",
                "owner": "fallback-owner",
                "repo": "fallback-repo",
                "forgejo_api_base": "https://forgejo.example.com/api/v1",
            },
        ):
            resolved = self.mod._with_git_defaults(target)

        self.assertEqual(resolved.provider, "github")
        self.assertEqual(resolved.owner, "explicit-owner")
        self.assertEqual(resolved.repo, "explicit-repo")


if __name__ == "__main__":
    unittest.main()
