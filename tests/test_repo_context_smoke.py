"""Unit tests for kinetic_devops.repo_context smoke helpers."""

import unittest
from unittest.mock import patch

from kinetic_devops import repo_context


class TestRepoContextSmoke(unittest.TestCase):
    def test_detect_provider_from_git_returns_provider(self):
        with patch(
            "kinetic_devops.repo_context.detect_from_git",
            return_value={
                "provider": "forgejo",
                "host": "forgejo.local",
                "owner": "acme",
                "repo": "project",
                "scheme": "https",
            },
        ):
            self.assertEqual(repo_context.detect_provider_from_git(), "forgejo")

    def test_detect_provider_from_git_uses_passed_error_type(self):
        class CustomError(RuntimeError):
            pass

        with patch(
            "kinetic_devops.repo_context.detect_from_git",
            side_effect=CustomError("git detect failed"),
        ):
            with self.assertRaises(CustomError):
                repo_context.detect_provider_from_git(error_type=CustomError)

    def test_detect_from_git_does_not_misclassify_lookalike_host(self):
        """host.endswith("github.com") would wrongly match "evilgithub.com"
        (no dot boundary) -- regression test for the fixed check."""
        with patch(
            "kinetic_devops.repo_context.subprocess.check_output",
            return_value="https://evilgithub.com/acme/project.git\n",
        ):
            detected = repo_context.detect_from_git()
        self.assertEqual(detected["provider"], "forgejo")

    def test_detect_from_git_classifies_github_subdomain(self):
        with patch(
            "kinetic_devops.repo_context.subprocess.check_output",
            return_value="https://ghe.github.com/acme/project.git\n",
        ):
            detected = repo_context.detect_from_git()
        self.assertEqual(detected["provider"], "github")


class TestRemoteHostIdentity(unittest.TestCase):
    """The host is the bare host name: user info and port never reach it (review finding #14)."""

    def test_user_and_port_do_not_change_the_host(self):
        for url in (
            "ssh://git@github.com/o/r.git",
            "https://user@github.com/o/r.git",
            "https://github.com:443/o/r",
            "https://x:tok123@github.com/o/r",
            "git@github.com:o/r.git",
            "https://GitHub.com/o/r",
        ):
            with self.subTest(url=url):
                parsed = repo_context.parse_git_remote(url)
                self.assertEqual(parsed["host"], "github.com")
                self.assertEqual((parsed["owner"], parsed["repo"]), ("o", "r"))

    def test_token_in_remote_url_does_not_reach_any_field(self):
        parsed = repo_context.parse_git_remote("https://x:tok123@forgejo.example.com:3000/o/r.git")
        self.assertNotIn("tok123", repr(parsed))
        with self.assertRaises(RuntimeError) as ctx:
            repo_context.parse_git_remote("https://x:tok123@forgejo.example.com/only-owner")
        self.assertNotIn("tok123", str(ctx.exception))

    def test_ssh_remote_with_user_is_github(self):
        with patch(
            "kinetic_devops.repo_context.subprocess.check_output",
            return_value="ssh://git@github.com/acme/project.git\n",
        ):
            self.assertEqual(repo_context.detect_from_git()["provider"], "github")

    def test_web_port_is_kept_for_api_urls_not_in_host(self):
        parsed = repo_context.parse_git_remote("https://forgejo.example.com:3000/o/r.git")
        self.assertEqual(parsed["host"], "forgejo.example.com")
        self.assertEqual(parsed["port"], "3000")
        self.assertEqual(parsed["authority"], "forgejo.example.com:3000")
        self.assertEqual(parsed["scheme"], "https")

    def test_ssh_port_is_not_a_web_port(self):
        parsed = repo_context.parse_git_remote("ssh://git@forgejo.example.com:2222/o/r.git")
        self.assertEqual((parsed["host"], parsed["port"], parsed["authority"], parsed["scheme"]),
                         ("forgejo.example.com", "", "forgejo.example.com", "https"))

    def test_host_and_authority_from_url(self):
        url = "https://x:tok@Forgejo.example.com:3000/api/v1"
        self.assertEqual(repo_context.host_from_url(url), "forgejo.example.com")
        self.assertEqual(repo_context.authority_from_url(url), "forgejo.example.com:3000")
        self.assertEqual(repo_context.authority_from_url("https://forgejo.example.com/api/v1"),
                         "forgejo.example.com")

    def test_keyring_tries_port_accounts_then_bare_host(self):
        with patch("keyring.get_password", return_value=None):
            _, _, accounts = repo_context.resolve_token(
                env_name="KINETIC_TEST_UNSET_TOKEN_ENV",
                token_service="svc",
                token_account="",
                host="forgejo.example.com:3000",
                owner="acme",
                legacy_account="forgejo",
            )
        self.assertEqual(accounts, [
            "forgejo.example.com:3000/acme",
            "forgejo.example.com/acme",
            "forgejo.example.com:3000",
            "forgejo.example.com",
            "forgejo",
        ])

    def test_apply_engine_builds_forgejo_api_base_with_web_port(self):
        from kinetic_devops.repomaker import apply_engine

        with patch(
            "kinetic_devops.repomaker.apply_engine.subprocess.check_output",
            return_value="https://user@forgejo.example.com:3000/acme/project.git\n",
        ):
            detected = apply_engine._detect_current_repo_from_git()
        self.assertEqual(detected["provider"], "forgejo")
        self.assertEqual(detected["forgejo_api_base"], "https://forgejo.example.com:3000/api/v1")


if __name__ == "__main__":
    unittest.main()
