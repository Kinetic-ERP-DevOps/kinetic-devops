"""Regression tests to keep CLI coverage matrix in sync with package entrypoints."""

import re
import unittest
from pathlib import Path

from kinetic_devops import __main__ as router
from tests.cli_matrix import CLI_MODULES, ROUTER_TOOLS


MAIN_GUARD_PATTERN = re.compile(r"if\s+__name__\s*==\s*['\"]__main__['\"]\s*:")


class TestCliMatrixConsistency(unittest.TestCase):
    """Ensure test matrix tracks all supported CLI entrypoints."""

    def test_matrix_matches_root_cli_modules_with_main_guard(self):
        repo_root = Path(__file__).resolve().parents[1]
        package_dir = repo_root / "kinetic_devops"

        discovered = {
            path.name
            for path in package_dir.glob("*.py")
            if path.name != "__main__.py"
            and MAIN_GUARD_PATTERN.search(path.read_text(encoding="utf-8", errors="replace"))
        }

        matrix_scripts = {item["script"] for item in CLI_MODULES}
        self.assertEqual(
            discovered,
            matrix_scripts,
            msg=(
                "CLI matrix is out of sync with package scripts that expose a __main__ guard. "
                "Update tests/cli_matrix.py to match."
            ),
        )

    def test_router_tool_mapping_matches_router_module(self):
        matrix_router_tools = {item["router"] for item in ROUTER_TOOLS}
        actual_router_tools = set(router.TOOLS.keys())

        self.assertEqual(
            matrix_router_tools,
            actual_router_tools,
            msg=(
                "Router tool list in tests/cli_matrix.py is out of sync with kinetic_devops.__main__.TOOLS. "
                "Update matrix mappings to keep router path coverage complete."
            ),
        )


if __name__ == "__main__":
    unittest.main()
