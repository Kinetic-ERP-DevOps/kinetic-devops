"""Regression tests for direct script execution of CLI modules.

These tests guard against relative-import breakage when running module files
as scripts, e.g. ``python kinetic_devops/solutions.py --help``.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.cli_matrix import CLI_MODULES, scaled_timeout


class TestDirectScriptCli(unittest.TestCase):
    """Verify CLI modules can run via direct file path."""

    def test_direct_script_help_for_all_cli_modules(self):
        repo_root = Path(__file__).resolve().parents[1]
        package_dir = repo_root / "kinetic_devops"

        for item in CLI_MODULES:
            script_name = item["script"]
            module_name = item["module"]
            with self.subTest(module=module_name):
                script_path = package_dir / script_name
                self.assertTrue(script_path.is_file(), f"Missing script: {script_path}")

                env = os.environ.copy()
                existing_pythonpath = env.get("PYTHONPATH", "")
                env["PYTHONPATH"] = (
                    str(repo_root)
                    if not existing_pythonpath
                    else f"{str(repo_root)}{os.pathsep}{existing_pythonpath}"
                )

                result = subprocess.run(
                    [sys.executable, str(script_path), "--help"],
                    cwd=str(repo_root),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=scaled_timeout(15),
                )

                self.assertEqual(
                    result.returncode,
                    0,
                    msg=(
                        f"{module_name} direct execution failed\n"
                        f"stdout:\n{result.stdout}\n"
                        f"stderr:\n{result.stderr}"
                    ),
                )
                self.assertIn("usage:", result.stdout.lower(), f"{module_name} help missing usage output")


if __name__ == "__main__":
    unittest.main()
