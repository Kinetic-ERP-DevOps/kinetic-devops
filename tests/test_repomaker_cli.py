"""Regression tests for kinetic_devops.repomaker modular CLI."""

from __future__ import annotations

import io
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from kinetic_devops.repomaker import __main__ as repomaker_cli
from kinetic_devops.repomaker import apply as repomaker_apply
from kinetic_devops.repomaker import apply_engine as repomaker_apply_engine
from kinetic_devops.repomaker import reposmith as repomaker_reposmith


class TestRepoMakerCli(unittest.TestCase):
    """Validate modular command routing and wrapper contracts."""

    def test_help_returns_zero_and_prints_usage(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = repomaker_cli.main(["--help"])

        self.assertEqual(code, 0)
        self.assertIn("repomaker apply", stdout.getvalue())
        self.assertIn("repomaker reposmith", stdout.getvalue())

    def test_apply_command_dispatches_to_apply_module(self):
        with patch.object(repomaker_cli.apply, "main", return_value=0) as apply_main:
            code = repomaker_cli.main(["apply", "--config", "x.json"])

        self.assertEqual(code, 0)
        apply_main.assert_called_once_with(["--config", "x.json"])

    def test_reposmith_aliases_dispatch_to_reposmith_module(self):
        with patch.object(repomaker_cli.reposmith, "main", return_value=0) as reposmith_main:
            code_reposmith = repomaker_cli.main(["reposmith", "--apply"])
            code_smoke = repomaker_cli.main(["smoke", "--apply"])

        self.assertEqual(code_reposmith, 0)
        self.assertEqual(code_smoke, 0)
        self.assertEqual(reposmith_main.call_count, 2)

    def test_command_is_case_insensitive_and_passes_help_through(self):
        with patch.object(repomaker_cli.apply, "main", return_value=0) as apply_main:
            code = repomaker_cli.main(["APPLY", "-h"])

        self.assertEqual(code, 0)
        apply_main.assert_called_once_with(["-h"])

    def test_unknown_command_returns_two(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = repomaker_cli.main(["unknown"])

        self.assertEqual(code, 2)
        self.assertIn("Unknown RepoMaker command", stderr.getvalue())

    def test_apply_wrapper_delegates_to_loaded_script_module(self):
        fake_module = types.SimpleNamespace(main=lambda argv=None: 17)

        with patch.object(repomaker_apply, "_load_apply_script_module", return_value=fake_module):
            code = repomaker_apply.main(["--config", "targets.json"])

        self.assertEqual(code, 17)

    def test_reposmith_wrapper_delegates_to_repo_maker_main(self):
        with patch.object(repomaker_reposmith.repo_maker, "main", return_value=9) as repo_maker_main:
            code = repomaker_reposmith.main(["--apply"])

        self.assertEqual(code, 9)
        repo_maker_main.assert_called_once_with(["--apply"])


class TestApplyEngineProviderDetection(unittest.TestCase):
    """host.endswith("github.com") would wrongly match a lookalike host like
    "evilgithub.com" (no dot boundary) -- regression tests for the fix."""

    def test_detect_current_repo_does_not_misclassify_lookalike_host(self):
        with patch.object(
            repomaker_apply_engine.subprocess,
            "check_output",
            return_value="https://evilgithub.com/acme/project.git\n",
        ):
            detected = repomaker_apply_engine._detect_current_repo_from_git()
        self.assertEqual(detected["provider"], "forgejo")

    def test_detect_current_repo_classifies_github_subdomain(self):
        with patch.object(
            repomaker_apply_engine.subprocess,
            "check_output",
            return_value="https://ghe.github.com/acme/project.git\n",
        ):
            detected = repomaker_apply_engine._detect_current_repo_from_git()
        self.assertEqual(detected["provider"], "github")


if __name__ == "__main__":
    unittest.main()
