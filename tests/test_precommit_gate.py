"""Regression tests for pre-commit hook gate wiring and dry-run behavior."""

import os
import subprocess
import sys
import unittest
from pathlib import Path


class TestPreCommitGate(unittest.TestCase):
    """Validate pre-commit gate can be verified via dry-run in tests."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]

    def test_precommit_script_supports_dry_run(self):
        script_path = self.repo_root / "scripts" / "hooks" / "pre-commit"
        result = subprocess.run(
            [sys.executable, str(script_path), "--dry-run"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "pre-commit --dry-run failed\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            ),
        )
        self.assertIn("PRE-COMMIT DRY RUN", result.stdout)
        self.assertIn("email privacy guard", result.stdout.lower())
        self.assertIn("sensitive data gate", result.stdout.lower())
        self.assertIn("DRY RUN RESULT: PASS", result.stdout)

    def test_git_hook_runner_can_execute_repo_precommit_in_dry_run(self):
        env = os.environ.copy()
        env["PRE_COMMIT_DRY_RUN"] = "1"

        # Force Git to use the repository hook path so this test is independent
        # from local/global hook installation state.
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=scripts/hooks", "hook", "run", "pre-commit"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "git hook run pre-commit dry-run failed\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            ),
        )
        output = f"{result.stdout}\n{result.stderr}"
        self.assertIn("PRE-COMMIT DRY RUN", output)
        self.assertIn("email privacy guard", output.lower())
        self.assertIn("sensitive data gate", output.lower())
        self.assertIn("DRY RUN RESULT: PASS", output)


if __name__ == "__main__":
    unittest.main()
