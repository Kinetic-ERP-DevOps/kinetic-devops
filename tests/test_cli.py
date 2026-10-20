"""CLI regression tests for all supported entry paths."""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.cli_matrix import CLI_MODULES, ROUTER_TOOLS, native_checks_enabled, scaled_timeout


class TestServiceCLI(unittest.TestCase):
    """Verify all modules are reachable through supported CLI entry paths."""

    @staticmethod
    def _run(command):
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=scaled_timeout(20),
        )

    @staticmethod
    def _assert_help(result, entry_name):
        if result.returncode != 0:
            raise AssertionError(
                f"{entry_name} --help failed with {result.returncode}\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )
        if "usage:" not in result.stdout.lower():
            raise AssertionError(
                f"{entry_name} --help missing usage output\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            )

    @staticmethod
    def _resolve_console_script():
        found = shutil.which("kinetic-devops")
        if found:
            return found

        repo_root = Path(__file__).resolve().parents[1]
        if os.name == "nt":
            candidates = [
                repo_root / ".venv" / "Scripts" / "kinetic-devops.exe",
                repo_root / ".venv" / "Scripts" / "kinetic-devops.cmd",
                repo_root / ".venv" / "Scripts" / "kinetic-devops",
            ]
        else:
            candidates = [repo_root / ".venv" / "bin" / "kinetic-devops"]

        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None

    def test_python_module_help_for_all_cli_modules(self):
        for item in CLI_MODULES:
            module_name = f"kinetic_devops.{item['module']}"
            with self.subTest(entry=module_name):
                result = self._run([sys.executable, "-m", module_name, "--help"])
                self._assert_help(result, module_name)
                self.assertNotIn("RuntimeWarning", result.stderr)

    def test_router_module_help_for_all_router_tools(self):
        for item in ROUTER_TOOLS:
            tool_name = item["router"]
            entry_name = f"python -m kinetic_devops {tool_name}"
            with self.subTest(entry=entry_name):
                result = self._run([sys.executable, "-m", "kinetic_devops", tool_name, "--help"])
                self._assert_help(result, entry_name)

    def test_console_script_help_for_all_router_tools(self):
        # Runs the generated console-script launcher (an unsigned .exe on
        # Windows), which endpoint security may block when freshly built, so
        # local runs defer it; CI, Windows Sandbox and `--native` runs execute it.
        # The same help paths are covered via `python -m` above.
        if not native_checks_enabled():
            self.skipTest("native check deferred (runs in CI, scripts/sandbox_test.py, or tests.test_runner --native)")
        script = self._resolve_console_script()
        if not script:
            self.skipTest("kinetic-devops console script was not found in PATH or .venv")

        for item in ROUTER_TOOLS:
            tool_name = item["router"]
            entry_name = f"kinetic-devops {tool_name}"
            with self.subTest(entry=entry_name):
                result = self._run([script, tool_name, "--help"])
                self._assert_help(result, entry_name)

    def test_router_module_root_help(self):
        result = self._run([sys.executable, "-m", "kinetic_devops", "--help"])
        self._assert_help(result, "python -m kinetic_devops")


if __name__ == "__main__":
    unittest.main()
