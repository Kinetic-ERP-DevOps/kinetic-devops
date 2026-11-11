"""Unit tests for kinetic_devops.repo_context smoke helpers."""

import io
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

    @staticmethod
    def _resolve(host, entries=None):
        entries = entries or {}
        with patch("keyring.get_password", side_effect=lambda svc, acct: entries.get(acct)):
            return repo_context.resolve_token(
                env_name="KINETIC_TEST_UNSET_TOKEN_ENV",
                token_service="svc",
                token_account="",
                host=host,
                owner="acme",
                legacy_account="forgejo",
            )

    def test_keyring_with_port_tries_only_port_accounts(self):
        # The bare host's accounts belong to the default-port server: never tried for host:port.
        _, _, accounts = self._resolve("forgejo.example.com:3000")
        self.assertEqual(accounts, ["forgejo.example.com:3000/acme", "forgejo.example.com:3000", "forgejo"])

    def test_keyring_without_port(self):
        _, _, accounts = self._resolve("forgejo.example.com")
        self.assertEqual(accounts, ["forgejo.example.com/acme", "forgejo.example.com", "forgejo"])
        _, _, accounts = self._resolve("user@forgejo.example.com")
        self.assertEqual(accounts, ["forgejo.example.com/acme", "forgejo.example.com", "forgejo"])

    def test_keyring_does_not_send_default_port_servers_token_to_another_port(self):
        entries = {
            "forgejo.example.com/acme": "token-for-default-port-server",
            "forgejo.example.com:3000": "token-for-port-3000-server",
        }
        token, source, _ = self._resolve("forgejo.example.com:3000", entries)
        self.assertEqual(token, "token-for-port-3000-server")
        self.assertEqual(source, "keyring:svc/forgejo.example.com:3000")
        token, _, _ = self._resolve("forgejo.example.com", entries)
        self.assertEqual(token, "token-for-default-port-server")

    def test_password_never_reaches_error_messages(self):
        # Splitting the authority at the first "/" leaked a password containing "/".
        for password in ("pa/ss", "pa@ss", "pa:ss", "pa%40ss", "p/a:s@s%40"):
            for template in ("https://user:{}@forgejo.example.com/o/r",
                             "https://user:{}@forgejo.example.com/only-owner",
                             "https://user:{}@forgejo.example.com:bad/o/r",
                             "https://user:{}@[::1/o/r"):
                url = template.format(password)
                with self.subTest(url=url):
                    self.assertNotIn(password, repo_context.redact_url(url))
                    try:
                        repo_context.parse_git_remote(url)
                    except RuntimeError as exc:
                        self.assertNotIn(password, str(exc))
                    for func in (repo_context.host_from_url, repo_context.authority_from_url):
                        try:
                            func(url)
                        except RuntimeError as exc:
                            self.assertNotIn(password, str(exc))

    def test_redact_url(self):
        self.assertEqual(repo_context.redact_url("https://u:p/w@forgejo.example.com/o/r"),
                         "https://***@forgejo.example.com/o/r")
        self.assertEqual(repo_context.redact_url("https://forgejo.example.com/o/r"),
                         "https://forgejo.example.com/o/r")
        self.assertEqual(repo_context.redact_url("git@forgejo.example.com:o/r.git"),
                         "***@forgejo.example.com:o/r.git")
        # query/fragment dropped; an "@" after a "?" makes the user info ambiguous
        self.assertEqual(repo_context.redact_url("https://h.example.com/o/r?access_token=t"),
                         "https://h.example.com/o/r")
        self.assertEqual(repo_context.redact_url("https://u:p?w@h.example.com/o/r"), "<URL redacted>")

    def test_invalid_ipv6_raises_the_callers_error_type(self):
        class CustomError(Exception):
            pass

        for func in (repo_context.parse_git_remote, repo_context.host_from_url,
                     repo_context.authority_from_url):
            with self.subTest(func=func.__name__), self.assertRaises(CustomError):
                func("https://x:secret@[::1/o/r", error_type=CustomError)

    def test_scp_like_follows_git(self):
        cases = {
            "git@forgejo.example.com:o/r.git": ("forgejo.example.com", "forgejo.example.com"),
            "myalias:o/r.git": ("myalias", "myalias"),          # ssh-config alias, no user@
            "git@[::1]:o/r.git": ("::1", "[::1]"),
            "[::1]:o/r.git": ("::1", "[::1]"),
            "[git@forgejo.example.com:2222]:o/r.git": ("forgejo.example.com", "forgejo.example.com"),
            "git@[forgejo.example.com:2222]:o/r.git": ("forgejo.example.com", "forgejo.example.com"),
            "a@b@forgejo.example.com:o/r": ("forgejo.example.com", "forgejo.example.com"),
        }
        for url, (host, authority) in cases.items():
            with self.subTest(url=url):
                parsed = repo_context.parse_git_remote(url)
                self.assertEqual((parsed["host"], parsed["authority"], parsed["port"], parsed["scheme"]),
                                 (host, authority, "", "https"))
                self.assertEqual((parsed["owner"], parsed["repo"]), ("o", "r"))

    def test_local_paths_and_malformed_scp_are_not_hosts(self):
        for url in (
            "./foo@bar:baz/o/r",      # a "/" before the ":": a local path in git
            "/srv/git@host:o/r",
            "C:\\repos\\o\\r",
            "C:/repos/o/r",
            "[git@forgejo.example.com:o/r.git",   # unclosed bracket
            "[forgejo.example.com]o/r.git",       # no ":" after the bracket
            "git@:o/r",
            ":o/r",
        ):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                repo_context.parse_git_remote(url)

    def test_apply_engine_builds_forgejo_api_base_with_web_port(self):
        from kinetic_devops.repomaker import apply_engine

        with patch(
            "kinetic_devops.repomaker.apply_engine.subprocess.check_output",
            return_value="https://user@forgejo.example.com:3000/acme/project.git\n",
        ):
            detected = apply_engine._detect_current_repo_from_git()
        self.assertEqual(detected["provider"], "forgejo")
        self.assertEqual(detected["forgejo_api_base"], "https://forgejo.example.com:3000/api/v1")


    def test_legacy_account_use_is_announced(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            token, source, _ = self._resolve("forgejo.example.com:3000", {"forgejo": "legacy-token"})
        self.assertEqual((token, source), ("legacy-token", "keyring:svc/forgejo"))
        self.assertIn("legacy keyring account svc/forgejo for forgejo.example.com:3000", err.getvalue())
        err = io.StringIO()
        with patch("sys.stderr", err):
            self._resolve("forgejo.example.com:3000", {"forgejo.example.com:3000": "own-token"})
        self.assertEqual(err.getvalue(), "")                      # a server's own token: nothing to say
        err = io.StringIO()
        with patch("keyring.get_password", side_effect=lambda svc, acct: {"github": "gh"}.get(acct)), \
             patch("sys.stderr", err):
            repo_context.resolve_token(env_name="KINETIC_TEST_UNSET_TOKEN_ENV", token_service="svc",
                                       token_account="", host="github.com", owner="acme", legacy_account="github")
        self.assertEqual(err.getvalue(), "")                      # github.com is the github account's own host

    def test_parse_errors_carry_no_chained_password_fragment(self):
        # urlparse's own message quotes the bad "port", i.e. part of a password containing "/".
        for func in (repo_context.parse_git_remote, repo_context.host_from_url, repo_context.authority_from_url):
            with self.subTest(func=func.__name__):
                with self.assertRaises(RuntimeError) as ctx:
                    func("https://user:ab/cd@forgejo.example.com/o/r")
                self.assertIsNone(ctx.exception.__cause__)
                self.assertTrue(ctx.exception.__suppress_context__)

    def test_repo_maker_looks_up_the_token_for_the_server_it_calls(self):
        from types import SimpleNamespace

        from kinetic_devops import repo_maker

        detected = {"provider": "forgejo", "host": "origin.example.com", "authority": "origin.example.com",
                    "owner": "me", "scheme": "https"}
        with patch.object(repo_context, "detect_from_git", return_value=detected), \
             patch.dict("os.environ", {"FORGEJO_OWNER": ""}):
            args = SimpleNamespace(owner="", forgejo_url="https://other.example.com:3000")
            owner, host, api_base = repo_maker._resolve_context("forgejo", args)
            self.assertEqual((owner, host, api_base), ("me", "other.example.com:3000",
                                                       "https://other.example.com:3000/api/v1"))
            args = SimpleNamespace(owner="", forgejo_url="")
            self.assertEqual(repo_maker._resolve_context("forgejo", args),
                             ("me", "origin.example.com", "https://origin.example.com/api/v1"))


if __name__ == "__main__":
    unittest.main()
