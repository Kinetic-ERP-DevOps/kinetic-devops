"""Regression tests for KineticConfigManager.get_active_config's environment scoping.

Covers the bug where an explicitly-named environment with zero or multiple
recorded sessions would silently fall back to prompt_for_env()'s global
"last active" pointer instead of staying scoped to the requested environment.
"""

import os
import sys
import unittest
from unittest.mock import patch

from kinetic_devops.auth import KineticConfigManager, SessionResolutionError


def _bare_manager() -> KineticConfigManager:
    """Construct a KineticConfigManager without running __init__ (which
    touches the keyring for salt derivation)."""
    return KineticConfigManager.__new__(KineticConfigManager)


class TestGetActiveConfigScoping(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("KIN_USER", None)
        # Default both streams to non-interactive so the "fails closed"
        # tests exercise that branch regardless of whether this suite
        # happens to run attached to a real terminal. The two interactive
        # tests override these explicitly within their own `with` blocks.
        self._stdin_isatty_patch = patch.object(sys.stdin, "isatty", return_value=False)
        self._stdout_isatty_patch = patch.object(sys.stdout, "isatty", return_value=False)
        self._stdin_isatty_patch.start()
        self._stdout_isatty_patch.start()

    def tearDown(self) -> None:
        os.environ.pop("KIN_USER", None)
        self._stdin_isatty_patch.stop()
        self._stdout_isatty_patch.stop()

    def test_explicit_env_single_session_resolves_without_prompting(self):
        mgr = _bare_manager()
        servers = {
            "pilot": {
                "url": "https://pilot.example",
                "api_key": "key",
                "company": "ACME",
                "companies": "ACME",
                "sessions": ["alice"],
            }
        }
        with patch.object(mgr, "_get_server_dict", return_value=servers), \
             patch.object(mgr, "_get_token_key", return_value="slot"), \
             patch.object(mgr, "_get_token_meta", return_value={"_is_valid": True, "AccessToken": "tok"}), \
             patch.object(mgr, "prompt_for_env") as mock_prompt:
            result = mgr.get_active_config("pilot", fields=("url", "token", "api_key", "company", "nickname"))

        mock_prompt.assert_not_called()
        self.assertEqual(result[3], "ACME")
        self.assertEqual(result[4], "pilot")

    def test_explicit_env_zero_sessions_fails_closed_without_drifting(self):
        mgr = _bare_manager()
        servers = {"pilot": {"url": "https://pilot.example", "api_key": "key", "companies": "ACME", "sessions": []}}
        with patch.object(mgr, "_get_server_dict", return_value=servers), \
             patch.object(mgr, "prompt_for_env") as mock_prompt:
            with self.assertRaises(SessionResolutionError) as ctx:
                mgr.get_active_config("pilot", fields=("url", "token"))

        mock_prompt.assert_not_called()
        self.assertIn("pilot", str(ctx.exception))
        self.assertIn("no sessions recorded", str(ctx.exception))

    def test_explicit_env_multiple_sessions_fails_closed_without_drifting(self):
        mgr = _bare_manager()
        servers = {
            "pilot": {"url": "https://pilot.example", "api_key": "key", "companies": "ACME", "sessions": ["alice", "bob"]}
        }
        with patch.object(mgr, "_get_server_dict", return_value=servers), \
             patch.object(mgr, "prompt_for_env") as mock_prompt:
            with self.assertRaises(SessionResolutionError) as ctx:
                mgr.get_active_config("pilot", fields=("url", "token"))

        mock_prompt.assert_not_called()
        self.assertIn("multiple sessions recorded", str(ctx.exception))
        # Must not leak the actual usernames into the error message.
        self.assertNotIn("alice", str(ctx.exception))
        self.assertNotIn("bob", str(ctx.exception))

    def test_explicit_env_zero_sessions_interactive_prompts_scoped_login(self):
        """Interactive + no sessions: prompt for a new login scoped to the
        named environment, never fall back to the global picker."""
        mgr = _bare_manager()
        servers = {"pilot": {"url": "https://pilot.example", "api_key": "key", "companies": "ACME", "sessions": []}}
        with patch.object(mgr, "_get_server_dict", return_value=servers), \
             patch.object(mgr, "_select_session_for_env", return_value=("newuser", "ACME")) as mock_select, \
             patch.object(mgr, "_get_token_key", return_value="slot"), \
             patch.object(mgr, "_get_token_meta", return_value=None), \
             patch.object(mgr, "_fetch_token_kinetic", return_value="tok"), \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(sys.stdout, "isatty", return_value=True), \
             patch.object(mgr, "prompt_for_env") as mock_prompt:
            mgr.get_active_config("pilot", fields=("url", "nickname"))

        mock_prompt.assert_not_called()
        mock_select.assert_called_once_with("pilot", servers["pilot"], preferred_company="")

    def test_explicit_company_survives_interactive_session_prompt(self):
        """An explicitly-supplied company (e.g. via --company, arriving as
        the 3rd element of a tuple context) must not be overwritten by the
        interactive session-selection prompt for an ambiguous session."""
        mgr = _bare_manager()
        servers = {
            "pilot": {"url": "https://pilot.example", "api_key": "key", "companies": "ACME,OTHER", "sessions": []}
        }
        with patch.object(mgr, "_get_server_dict", return_value=servers), \
             patch.object(mgr, "_select_session_for_env", return_value=("newuser", "ACME")) as mock_select, \
             patch.object(mgr, "_get_token_key", return_value="slot"), \
             patch.object(mgr, "_get_token_meta", return_value=None), \
             patch.object(mgr, "_fetch_token_kinetic", return_value="tok"), \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(sys.stdout, "isatty", return_value=True):
            result = mgr.get_active_config(("pilot", "", "ACME"), fields=("company",))

        mock_select.assert_called_once_with("pilot", servers["pilot"], preferred_company="ACME")
        self.assertEqual(result, ("ACME",))

    def test_explicit_env_multiple_sessions_interactive_prompts_scoped_pick(self):
        """Interactive + multiple sessions: prompt to pick among THIS
        environment's sessions, never fall back to the global picker."""
        mgr = _bare_manager()
        servers = {
            "pilot": {"url": "https://pilot.example", "api_key": "key", "companies": "ACME", "sessions": ["alice", "bob"]}
        }
        with patch.object(mgr, "_get_server_dict", return_value=servers), \
             patch.object(mgr, "_select_session_for_env", return_value=("bob", "ACME")) as mock_select, \
             patch.object(mgr, "_get_token_key", return_value="slot"), \
             patch.object(mgr, "_get_token_meta", return_value={"_is_valid": True, "AccessToken": "tok"}), \
             patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(sys.stdout, "isatty", return_value=True), \
             patch.object(mgr, "prompt_for_env") as mock_prompt:
            result = mgr.get_active_config("pilot", fields=("url", "nickname"))

        mock_prompt.assert_not_called()
        mock_select.assert_called_once_with("pilot", servers["pilot"], preferred_company="")
        self.assertEqual(result[1], "pilot")

    def test_kin_user_env_var_disambiguates_without_raising(self):
        mgr = _bare_manager()
        servers = {
            "pilot": {
                "url": "https://pilot.example",
                "api_key": "key",
                "company": "ACME",
                "companies": "ACME",
                "sessions": ["alice", "bob"],
            }
        }
        os.environ["KIN_USER"] = "bob"
        with patch.object(mgr, "_get_server_dict", return_value=servers), \
             patch.object(mgr, "_get_token_key", return_value="slot"), \
             patch.object(mgr, "_get_token_meta", return_value={"_is_valid": True, "AccessToken": "tok"}), \
             patch.object(mgr, "prompt_for_env") as mock_prompt:
            result = mgr.get_active_config("pilot", fields=("url", "nickname"))

        mock_prompt.assert_not_called()
        self.assertEqual(result[1], "pilot")

    def test_explicit_user_in_context_wins_over_kin_user(self):
        mgr = _bare_manager()
        servers = {
            "pilot": {
                "url": "https://pilot.example",
                "api_key": "key",
                "company": "ACME",
                "companies": "ACME",
                "sessions": ["alice", "bob"],
            }
        }
        os.environ["KIN_USER"] = "bob"
        with patch.object(mgr, "_get_server_dict", return_value=servers), \
             patch.object(mgr, "_get_token_key", return_value="slot") as mock_token_key, \
             patch.object(mgr, "_get_token_meta", return_value={"_is_valid": True, "AccessToken": "tok"}), \
             patch.object(mgr, "prompt_for_env") as mock_prompt:
            mgr.get_active_config(("pilot", "alice"), fields=("url",))

        mock_prompt.assert_not_called()
        mock_token_key.assert_called_once_with("pilot", "alice", "key")

    def test_no_env_name_falls_back_to_global_last_active(self):
        """The only path allowed to resolve a different environment than asked
        for is the one where no environment name was given at all.

        Every real caller (use(), KineticBaseClient.__init__, the CLI
        scripts) already calls prompt_for_env() itself before ever invoking
        get_active_config() with an empty context, so this exercises the
        branch directly rather than through a realistic caller.
        """
        mgr = _bare_manager()
        # Registered under the empty-string key so _find_env can still
        # resolve a cfg with a falsy env_name, reaching the fallback branch.
        # Two sessions (not one) so the initial resolution attempt can't take
        # the single-session auto-pick shortcut either -- it must go through
        # prompt_for_env(). "someone" needs to already be a recorded session
        # so the downstream cached-token reuse check succeeds without
        # triggering an interactive token fetch.
        servers = {"": {"url": "https://fallback.example", "api_key": "key", "companies": "ACME", "sessions": ["someone", "other"]}}
        with patch.object(mgr, "_get_server_dict", return_value=servers), \
             patch.object(mgr, "prompt_for_env", return_value=("", "someone", "ACME")) as mock_prompt, \
             patch.object(mgr, "_get_token_key", return_value="slot"), \
             patch.object(mgr, "_get_token_meta", return_value={"_is_valid": True, "AccessToken": "tok"}):
            result = mgr.get_active_config("", fields=("url",))

        mock_prompt.assert_called_once()
        self.assertEqual(result, ("https://fallback.example",))


if __name__ == "__main__":
    unittest.main()
