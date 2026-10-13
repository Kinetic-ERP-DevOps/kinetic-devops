"""Consolidated regression tests for scripts/repo_maker.py."""

import io
import importlib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

class TestRepoProviderFullstackSmoke(unittest.TestCase):
    """Verify common argument handling and dry-run behavior for both providers."""

    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("kinetic_devops.repo_maker")

    def test_github_payload(self):
        payload = self.mod.build_branch_protection_payload("github", "main", "Python Test Gate", 2)
        self.assertTrue(payload["required_status_checks"]["strict"])
        self.assertEqual(payload["required_status_checks"]["contexts"], ["Python Test Gate"])
        self.assertEqual(payload["required_pull_request_reviews"]["required_approving_review_count"], 2)

    def test_forgejo_payload(self):
        payload = self.mod.build_branch_protection_payload("forgejo", "main", "Python Test Gate", 2)
        self.assertTrue(payload["enable_status_check"])
        self.assertEqual(payload["status_check_contexts"], ["Python Test Gate"])
        self.assertEqual(payload["required_approvals"], 2)

    def test_github_api_base_defaults(self):
        self.assertEqual(self.mod.github_api_base("github.com"), "https://api.github.com")
        self.assertEqual(self.mod.github_api_base(""), "https://api.github.com")

    def test_auto_provider_dispatches_to_github(self):
        with patch.object(self.mod.repo_context, "detect_provider_from_git", return_value="github"):
            with patch.object(self.mod, "run_smoke") as run_smoke:
                code = self.mod.main(["--provider", "auto", "--owner", "acme"])

        self.assertEqual(code, 0)
        self.assertEqual(run_smoke.call_args.args[0].provider, "github")

    def test_auto_provider_dispatches_to_forgejo(self):
        with patch.object(self.mod.repo_context, "detect_provider_from_git", return_value="forgejo"):
            with patch.object(self.mod, "run_smoke") as run_smoke:
                code = self.mod.main([
                    "--provider",
                    "auto",
                    "--owner",
                    "acme",
                    "--forgejo-url",
                    "https://forgejo.local",
                ])

        self.assertEqual(code, 0)
        self.assertEqual(run_smoke.call_args.args[0].provider, "forgejo")

    def test_forgejo_url_is_forwarded_to_config(self):
        with patch.object(self.mod, "run_smoke") as run_smoke:
            code = self.mod.main([
                "--provider",
                "forgejo",
                "--owner",
                "acme",
                "--forgejo-url",
                "https://forgejo.local",
            ])

        self.assertEqual(code, 0)
        self.assertEqual(run_smoke.call_args.args[0].api_base, "https://forgejo.local/api/v1")

    def test_github_main_dry_run_passes_without_token(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict("os.environ", {"GITHUB_OWNER": "acme"}, clear=False):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = self.mod.main(["--provider", "github"])

        self.assertEqual(code, 0)
        self.assertIn("Mode: dry-run", stdout.getvalue())
        self.assertIn("SMOKE RESULT: PASS", stdout.getvalue())

    def test_forgejo_main_dry_run_passes_without_token(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict("os.environ", {"FORGEJO_OWNER": "acme"}, clear=False):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = self.mod.main(["--provider", "forgejo", "--forgejo-url", "https://forgejo.local"])

        self.assertEqual(code, 0)
        self.assertIn("Mode: dry-run", stdout.getvalue())
        self.assertIn("SMOKE RESULT: PASS", stdout.getvalue())

    def test_github_main_apply_requires_token(self):
        stderr = io.StringIO()
        with patch.dict("os.environ", {"GITHUB_OWNER": "acme"}, clear=True):
            with patch("keyring.get_password", return_value=None):
                with redirect_stderr(stderr):
                    code = self.mod.main(["--provider", "github", "--apply"])

        self.assertEqual(code, 1)
        self.assertIn("SMOKE RESULT: FAIL", stderr.getvalue())

    def test_forgejo_main_apply_requires_token(self):
        stderr = io.StringIO()
        with patch.dict("os.environ", {"FORGEJO_OWNER": "acme"}, clear=True):
            with patch("keyring.get_password", return_value=None):
                with redirect_stderr(stderr):
                    code = self.mod.main(["--provider", "forgejo", "--forgejo-url", "https://forgejo.local", "--apply"])

        self.assertEqual(code, 1)
        self.assertIn("SMOKE RESULT: FAIL", stderr.getvalue())

    def test_github_main_apply_uses_keyring_token(self):
        with patch.dict("os.environ", {"GITHUB_OWNER": "acme"}, clear=True):
            with patch("keyring.get_password", return_value="kr_token"):
                with patch.object(self.mod, "run_smoke") as run_smoke:
                    code = self.mod.main(["--provider", "github", "--apply"])

        self.assertEqual(code, 0)
        run_smoke.assert_called_once()

    def test_forgejo_main_apply_uses_keyring_token(self):
        with patch.dict("os.environ", {"FORGEJO_OWNER": "acme"}, clear=True):
            with patch("keyring.get_password", return_value="kr_token"):
                with patch.object(self.mod, "run_smoke") as run_smoke:
                    code = self.mod.main(["--provider", "forgejo", "--forgejo-url", "https://forgejo.local", "--apply"])

        self.assertEqual(code, 0)
        run_smoke.assert_called_once()

    def test_github_main_dry_run_infers_owner_from_git_remote(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(
                self.mod.repo_context,
                "detect_from_git",
                return_value={
                    "provider": "github",
                    "host": "github.com",
                    "owner": "acme",
                    "repo": "project",
                    "scheme": "https",
                },
            ):
                with patch.object(self.mod, "run_smoke") as run_smoke:
                    code = self.mod.main(["--provider", "github"])

        self.assertEqual(code, 0)
        cfg = run_smoke.call_args.args[0]
        self.assertEqual(cfg.owner, "acme")

    def test_forgejo_main_dry_run_infers_url_and_owner_from_git_remote(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(
                self.mod.repo_context,
                "detect_from_git",
                return_value={
                    "provider": "forgejo",
                    "host": "forgejo.local",
                    "owner": "acme",
                    "repo": "project",
                    "scheme": "https",
                },
            ):
                with patch.object(self.mod, "run_smoke") as run_smoke:
                    code = self.mod.main(["--provider", "forgejo"])

        self.assertEqual(code, 0)
        cfg = run_smoke.call_args.args[0]
        self.assertEqual(cfg.api_base, "https://forgejo.local/api/v1")
        self.assertEqual(cfg.owner, "acme")

    def test_github_main_without_explicit_owner_fails_on_non_github_remote(self):
        stderr = io.StringIO()
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(
                self.mod.repo_context,
                "detect_from_git",
                return_value={
                    "provider": "forgejo",
                    "host": "forgejo.local",
                    "owner": "acme",
                    "repo": "project",
                    "scheme": "https",
                },
            ):
                with redirect_stderr(stderr):
                    code = self.mod.main(["--provider", "github"])

        self.assertEqual(code, 1)
        self.assertIn("does not appear to be GitHub", stderr.getvalue())

    def test_forgejo_main_without_explicit_url_fails_on_github_remote(self):
        stderr = io.StringIO()
        with patch.dict("os.environ", {}, clear=True):
            with patch.object(
                self.mod.repo_context,
                "detect_from_git",
                return_value={
                    "provider": "github",
                    "host": "github.com",
                    "owner": "org",
                    "repo": "repo",
                    "scheme": "https",
                },
            ):
                with redirect_stderr(stderr):
                    code = self.mod.main(["--provider", "forgejo"])

        self.assertEqual(code, 1)
        self.assertIn("appears to be GitHub", stderr.getvalue())

    def test_bad_provider_returns_nonzero(self):
        stderr = io.StringIO()
        with patch.object(self.mod.repo_context, "detect_provider_from_git", return_value="bad"):
            with redirect_stderr(stderr):
                code = self.mod.main(["--provider", "auto"])

        self.assertEqual(code, 1)
        self.assertIn("Unsupported provider", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()