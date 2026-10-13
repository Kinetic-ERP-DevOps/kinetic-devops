"""CLI subprocess tests for custom layer content helper scripts."""

import json
import subprocess
import sys
import tempfile
import unittest

from tests.cli_matrix import scaled_timeout
from pathlib import Path


class TestCustomLayerContentScriptsCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.scripts_dir = cls.repo_root / "scripts"
        cls.build_script = cls.scripts_dir / "build_kinetic_custom_layer.py"
        cls.extract_script = cls.scripts_dir / "extract_and_format_content.py"

    def _run(self, script_path: Path, *args: str):
        return subprocess.run(
            [sys.executable, str(script_path), *args],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            timeout=scaled_timeout(15),
        )

    def test_build_script_usage_exits_nonzero_without_args(self):
        result = self._run(self.build_script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)

    def test_extract_script_usage_exits_nonzero_without_args(self):
        result = self._run(self.extract_script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)

    def test_build_script_missing_input_exits_nonzero(self):
        result = self._run(self.build_script, "does_not_exist.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error: The file", result.stdout)

    def test_extract_script_missing_input_exits_nonzero(self):
        result = self._run(self.extract_script, "does_not_exist.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error: The file", result.stdout)

    def test_extract_script_missing_content_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "missing_content.json"
            input_path.write_text(json.dumps({"App": "ExampleApp"}), encoding="utf-8")

            result = self._run(self.extract_script, str(input_path))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("'Content' key was not found or is empty", result.stdout)


if __name__ == "__main__":
    unittest.main()
