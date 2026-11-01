"""Regression tests for scripts/apply_branch_protection.py."""

import importlib.util
import io
import json
import os
import re
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
        supported = ["3.11", "3.12", "3.13", "3.14"]
        self.assertEqual(
            targets[0].required_checks,
            ["Sensitive Data Gate"] + [f"Python Test Gate (required, py{v})" for v in supported],
        )
        # Forgejo names checks "<workflow> / <job> (<event>)"; its target matches them with globs.
        self.assertEqual(
            targets[1].required_checks,
            ["CI Tests / Sensitive Data Gate *"]
            + [f"CI Tests / Python Test Gate (required, py{v}) *" for v in supported],
        )
        self.assertEqual(targets[0].github_mode, "ruleset")
        self.assertEqual(targets[0].protect_tags, ["v*"])

    def test_required_python_checks_match_ci_matrix(self):
        # Every Python version CI tests must be a required check (support policy: all supported versions).
        targets = self.mod._parse_targets(self.mod._load_config(Path("scripts/branch_protection.targets.example.json")))
        required = {c for c in targets[0].required_checks if c.startswith("Python Test Gate")}
        for path in (".github/workflows/ci-tests.yml", ".forgejo/workflows/ci-tests.yml"):
            workflow = Path(path).read_text(encoding="utf-8")
            versions = json.loads(re.search(r"python-version: (\[[^\]]*\])", workflow).group(1))
            self.assertEqual(required, {f"Python Test Gate (required, py{v})" for v in versions}, path)

    def test_committed_ruleset_json_matches_engine(self):
        # scripts/github-rulesets/*.json (for GitHub's ruleset import) must equal what the engine applies.
        targets = self.mod._parse_targets(self.mod._load_config(Path("scripts/branch_protection.targets.example.json")))
        gh = targets[0]
        for name, payload in (
            ("protect-main.json", self.mod._github_ruleset_payload(gh)),
            ("protect-main-tags.json", self.mod._github_tag_ruleset_payload(gh)),
        ):
            committed = json.loads(Path("scripts/github-rulesets", name).read_text(encoding="utf-8"))
            self.assertEqual(committed, payload, f"regenerate scripts/github-rulesets/{name}")

    def test_ruleset_payload(self):
        target = self.mod.Target(
            provider="github", owner="example", repo="repo", branch="main", token_env="GITHUB_TOKEN",
            required_checks=["Python Test Gate"], required_approvals=1, enforce_admins=False,
            require_conversation_resolution=True, forgejo_api_base="", checks_integration_id=15368,
        )
        payload = self.mod._github_ruleset_payload(target)
        self.assertEqual(payload["name"], "protect-main")
        self.assertEqual(payload["conditions"]["ref_name"]["include"], ["refs/heads/main"])
        rules = {r["type"]: r.get("parameters") for r in payload["rules"]}
        self.assertEqual(set(rules), {"deletion", "non_fast_forward", "pull_request", "required_status_checks"})
        self.assertEqual(rules["pull_request"]["required_approving_review_count"], 1)
        self.assertNotIn("allowed_merge_methods", rules["pull_request"])
        self.assertEqual(
            rules["required_status_checks"]["required_status_checks"],
            [{"context": "Python Test Gate", "integration_id": 15368}],
        )
        # enforce_admins=False keeps the classic meaning: repository admins may bypass.
        self.assertEqual(payload["bypass_actors"][0]["actor_type"], "RepositoryRole")

    def test_invalid_github_mode_rejected(self):
        with self.assertRaises(self.mod.BranchProtectionError):
            self.mod._merge_target({}, {"provider": "github", "github_mode": "legacy", "token_env": "X"})

    def test_apply_rulesets_updates_existing_by_name(self):
        target = self.mod.Target(
            provider="github", owner="example", repo="repo", branch="main", token_env="GITHUB_TOKEN",
            required_checks=["Python Test Gate"], required_approvals=0, enforce_admins=True,
            require_conversation_resolution=True, forgejo_api_base="", protect_tags=["v*"],
        )

        class Resp:
            def __init__(self, code, data=None):
                self.status_code, self._data, self.text = code, data, ""

            def json(self):
                return self._data

        existing = [{"id": 7, "name": "protect-main", "target": "branch", "source_type": "Repository"}]
        engine = sys.modules[self.mod._github_ruleset_payload.__module__]
        with patch.object(engine.requests, "get", side_effect=[Resp(200, existing), Resp(404)]), \
             patch.object(engine.requests, "put", return_value=Resp(200)) as put, \
             patch.object(engine.requests, "post", return_value=Resp(201)) as post, \
             redirect_stdout(io.StringIO()):
            self.mod._apply_github(target, token="t", dry_run=False)

        self.assertTrue(put.call_args.args[0].endswith("/rulesets/7"))
        self.assertEqual(post.call_args.kwargs["json"]["name"], "protect-main-tags")

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
        # The example leaves the Forgejo owner/repo blank so they come from the
        # checkout's origin remote; fill them in so the result doesn't depend
        # on how (or whether) this checkout has an origin.
        cfg = json.loads(
            Path("scripts/branch_protection.targets.example.json").read_text(encoding="utf-8")
        )
        for target in cfg["targets"]:
            if target["provider"] == "forgejo":
                target.update(
                    owner="example",
                    repo="kinetic-devops",
                    forgejo_api_base="https://forgejo.example.invalid/api/v1",
                )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "targets.json"
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = self.mod.main([
                    "--config",
                    str(cfg_path),
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
