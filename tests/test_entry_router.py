"""Regression tests for kinetic_devops entry routing behavior."""

import unittest
from unittest.mock import patch

from kinetic_devops import __main__ as router


class TestEntryRouter(unittest.TestCase):
    """Verify router behavior without shelling out to subprocesses."""

    def test_main_dispatches_known_tool_and_passes_args(self):
        captured = {}

        def fake_tool_main():
            captured["argv"] = list(__import__("sys").argv)

        with patch.dict(router.TOOLS, {"baq": fake_tool_main}, clear=True):
            exit_code = router.main(["baq", "--help"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["argv"][1:], ["--help"])
        self.assertTrue(captured["argv"][0].endswith(" baq"))

    def test_main_propagates_known_tool_exit_code(self):
        def fake_tool_main():
            return 7

        with patch.dict(router.TOOLS, {"scope": fake_tool_main}, clear=True):
            exit_code = router.main(["scope", "--invalid"])

        self.assertEqual(exit_code, 7)

    def test_main_returns_nonzero_when_no_arguments(self):
        with patch("sys.stdout"):
            exit_code = router.main([])
        self.assertEqual(exit_code, 1)

    def test_main_supports_router_level_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            router.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_pyproject_has_console_script_mapping(self):
        with open("pyproject.toml", "r", encoding="utf-8") as fh:
            pyproject_text = fh.read()

        self.assertIn("[project.scripts]", pyproject_text)
        self.assertIn('kinetic-devops = "kinetic_devops.__main__:main"', pyproject_text)


if __name__ == "__main__":
    unittest.main()
