"""Tests for custom layer content transform helper scripts."""

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


def _load_script_module(module_name: str, script_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(script_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCustomLayerContentScripts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.scripts_dir = cls.repo_root / "scripts"
        cls.build_module = _load_script_module(
            "build_kinetic_custom_layer",
            cls.scripts_dir / "build_kinetic_custom_layer.py",
        )
        cls.extract_module = _load_script_module(
            "extract_and_format_content",
            cls.scripts_dir / "extract_and_format_content.py",
        )

    def test_strip_jsonc_comments_removes_line_and_block_comments(self):
        jsonc_text = '{\n  // line comment\n  "a": 1,\n  /* block comment */\n  "b": 2\n}'
        result = self.build_module.strip_jsonc_comments(jsonc_text)

        self.assertIn('"a": 1', result)
        self.assertIn('"b": 2', result)
        self.assertNotIn("// line comment", result)
        self.assertNotIn("/* block comment */", result)

    def test_embed_and_escape_content_creates_expected_output(self):
        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "layer.jsonc"
            input_path.write_text(
                '{\n  // comment\n  "name": "x",\n  "count": 2\n}\n',
                encoding="utf-8",
            )

            message = self.build_module.embed_and_escape_content(str(input_path))
            self.assertIn("Successfully created new file", message)

            output_path = Path(td) / "layer_escaped.jsonc"
            self.assertTrue(output_path.exists(), "Escaped output file was not created")

            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["App"], "ExampleApp")
            self.assertEqual(payload["Operation"], "Merge")
            self.assertEqual(payload["TargetFile"], "/path/to/layer.jsonc")

            embedded_obj = json.loads(payload["Content"])
            self.assertEqual(embedded_obj, {"name": "x", "count": 2})

    def test_embed_and_escape_content_missing_file_returns_error_message(self):
        message = self.build_module.embed_and_escape_content("does_not_exist.json")
        self.assertIn("Error: The file", message)
        self.assertIn("was not found", message)

    def test_extract_and_format_content_writes_temp_file(self):
        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "layer_escaped.json"
            input_payload = {
                "App": "ExampleApp",
                "Operation": "Merge",
                "TargetFile": "/path/to/layer.jsonc",
                "Content": json.dumps({"name": "x", "count": 2}),
            }
            input_path.write_text(json.dumps(input_payload), encoding="utf-8")

            out = io.StringIO()
            with redirect_stdout(out):
                self.extract_module.extract_and_format_content(str(input_path))

            output_path = Path(td) / "layer_escaped.temp.jsonc"
            self.assertTrue(output_path.exists(), "Formatted output file was not created")
            formatted = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(formatted, {"name": "x", "count": 2})
            self.assertIn("Successfully extracted and formatted content", out.getvalue())

    def test_extract_and_format_content_exits_when_content_missing(self):
        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "missing_content.json"
            input_path.write_text(json.dumps({"App": "ExampleApp"}), encoding="utf-8")

            out = io.StringIO()
            with redirect_stdout(out):
                with self.assertRaises(SystemExit) as ctx:
                    self.extract_module.extract_and_format_content(str(input_path))

            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("'Content' key was not found or is empty", out.getvalue())


if __name__ == "__main__":
    unittest.main()
