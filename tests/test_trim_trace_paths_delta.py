import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kinetic_devops import trim_trace_paths


class TestTrimTracePathsDelta(unittest.TestCase):
    def _write_input(self, temp_dir: Path, content: dict) -> Path:
        src = temp_dir / "trace.json"
        src.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        return src

    def _run_process(
        self,
        src: Path,
        out_dir: Path,
        delta_dir: Path,
        **kwargs,
    ):
        ok, msg, detail = trim_trace_paths.process_file(
            src=src,
            drop_patterns=kwargs.get("drop_patterns", []),
            drop_keys=kwargs.get("drop_keys", set()),
            in_place=False,
            out_dir=str(out_dir),
            suffix="_reduced",
            pretty=True,
            dry_run=False,
            do_prune_empty=False,
            show_samples=0,
            show_top_rules=0,
            no_backup=True,
            drop_node_name_patterns=[],
            drop_parent_name_patterns=[],
            strip_empty_call_context=False,
            show_call_summary=False,
            sequence_only=False,
            redact_mode="aggressive",
            call_context_policy="purge",
            light_repair_json=True,
            sample_size=0,
            sample_include_patterns=[],
            sample_exclude_patterns=[],
            delta_mode=kwargs.get("delta_mode", "off"),
            delta_format=kwargs.get("delta_format", "jsonc"),
            delta_out_dir=str(delta_dir),
            delta_max_value_len=kwargs.get("delta_max_value_len", 200),
            delta_redact_values=kwargs.get("delta_redact_values", True),
        )
        return ok, msg, detail

    def test_parse_args_includes_delta_options(self):
        with patch("sys.argv", ["trim_trace_paths.py", "trace.json", "--delta-mode", "annotated", "--delta-format", "both"]):
            args = trim_trace_paths.parse_args()

        self.assertEqual(args.delta_mode, "annotated")
        self.assertEqual(args.delta_format, "both")
        self.assertEqual(args.delta_out_dir, "delta")
        self.assertEqual(args.delta_max_value_len, 200)
        self.assertTrue(args.delta_redact_values)

    def test_summary_delta_counts_and_jsonc_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            src = self._write_input(
                temp_dir,
                {
                    "Company": "Epicor",
                    "root": {
                        "removeMe": "secret",
                        "keep": "value",
                    },
                },
            )
            out_dir = temp_dir / "reduced"
            delta_dir = temp_dir / "delta"

            ok, msg, detail = self._run_process(
                src,
                out_dir,
                delta_dir,
                drop_patterns=["root.removeMe"],
                delta_mode="summary",
                delta_format="jsonc",
            )

            self.assertTrue(ok, msg)
            self.assertIn("delta=summary:jsonc", msg)
            self.assertTrue((out_dir / "trace_reduced.json").exists())

            sidecars = list(delta_dir.rglob("*.jsonc"))
            self.assertEqual(len(sidecars), 1)
            text = sidecars[0].read_text(encoding="utf-8")
            self.assertIn("// trace delta sidecar (summary)", text)
            payload_text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//")).strip()
            payload = json.loads(payload_text)
            self.assertEqual(payload["added"], 0)
            self.assertEqual(payload["removed"], 1)
            self.assertEqual(payload["modified"], 1)
            self.assertGreaterEqual(len(payload["top_changed_paths"]), 1)

    def test_annotated_delta_jsonl_emits_rules_and_redacted_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            src = self._write_input(
                temp_dir,
                {
                    "Company": "Epicor",
                    "root": {
                        "removeMe": "secret",
                        "items": ["alpha", "beta"],
                    },
                },
            )
            out_dir = temp_dir / "reduced"
            delta_dir = temp_dir / "delta"

            ok, msg, detail = self._run_process(
                src,
                out_dir,
                delta_dir,
                drop_patterns=["root.removeMe", "root.items[[]1[]]"],
                delta_mode="annotated",
                delta_format="jsonl",
            )

            self.assertTrue(ok, msg)
            self.assertIn("delta=annotated:jsonl", msg)

            sidecars = list(delta_dir.rglob("*.jsonl"))
            self.assertEqual(len(sidecars), 1)
            lines = [line for line in sidecars[0].read_text(encoding="utf-8").splitlines() if line.strip()]
            summary = json.loads(lines[0])
            events = [json.loads(line) for line in lines[1:]]

            self.assertEqual(summary["mode"], "annotated")
            self.assertEqual(summary["removed"], 2)
            self.assertEqual(summary["modified"], 1)
            company_event = next(event for event in events if event["path"] == "Company")
            item_event = next(event for event in events if event["path"] == "root.items[1]")
            remove_event = next(event for event in events if event["path"] == "root.removeMe")

            self.assertEqual(company_event["op"], "modify")
            self.assertEqual(item_event["op"], "remove")
            self.assertEqual(item_event["rule"], "root.items[[]1[]]")
            self.assertEqual(item_event["old"], "[REDACTED]")
            self.assertEqual(remove_event["op"], "remove")

    def test_delta_truncates_values_when_redaction_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            src = self._write_input(
                temp_dir,
                {
                    "root": {
                        "items": ["x" * 50, "y" * 50],
                    },
                },
            )
            out_dir = temp_dir / "reduced"
            delta_dir = temp_dir / "delta"

            ok, msg, detail = self._run_process(
                src,
                out_dir,
                delta_dir,
                drop_patterns=["root.items[[]1[]]"],
                delta_mode="annotated",
                delta_format="jsonl",
                delta_redact_values=False,
                delta_max_value_len=12,
            )

            self.assertTrue(ok, msg)
            sidecars = list(delta_dir.rglob("*.jsonl"))
            lines = [line for line in sidecars[0].read_text(encoding="utf-8").splitlines() if line.strip()]
            events = [json.loads(line) for line in lines[1:]]

            remove_event = next(event for event in events if event["path"] == "root.items[1]")
            self.assertIn("...[truncated]", remove_event["old"])
            self.assertNotIn("y" * 20, remove_event["old"])

    def test_annotated_jsonc_sidecar_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            src = self._write_input(
                temp_dir,
                {
                    "Company": "Epicor",
                    "root": {"removeMe": "secret"},
                },
            )
            out_dir = temp_dir / "reduced"
            delta_dir = temp_dir / "delta"

            ok, msg, detail = self._run_process(
                src,
                out_dir,
                delta_dir,
                drop_patterns=["root.removeMe"],
                delta_mode="annotated",
                delta_format="jsonc",
            )

            self.assertTrue(ok, msg)
            sidecars = list(delta_dir.rglob("*.jsonc"))
            self.assertEqual(len(sidecars), 1)
            text = sidecars[0].read_text(encoding="utf-8")
            self.assertIn("// trace delta sidecar (annotated)", text)
            self.assertIn("\"events\"", text)


if __name__ == "__main__":
    unittest.main()