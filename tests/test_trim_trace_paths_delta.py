import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
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
            redact_mode=kwargs.get("redact_mode", "aggressive"),
            call_context_policy=kwargs.get("call_context_policy", "purge"),
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
    def test_sensitive_values_mode_preserves_structure_and_redacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            src = self._write_input(
                temp_dir,
                {
                    "Company": "ZZTEST",
                    "UserID": "fakeuser",
                    "EMailAddress": "someone@example.com",
                    "note": "plain text stays",
                    "rows": [
                        {"Company": "ZZTEST", "Plant": "00", "keep": 1},
                        {"Company": "ZZTEST", "Plant": "01", "keep": 2},
                    ],
                },
            )
            out_dir = temp_dir / "reduced"
            delta_dir = temp_dir / "delta"

            ok, msg, detail = self._run_process(
                src,
                out_dir,
                delta_dir,
                redact_mode="sensitive-values",
            )

            self.assertTrue(ok, msg)
            self.assertIn("removed=0 nodes", msg)
            self.assertIn("structure=identical", msg)

            out = json.loads((out_dir / "trace_reduced.json").read_text(encoding="utf-8"))
            self.assertEqual(out["Company"], "{COMPANY_ID}")
            self.assertEqual(out["UserID"], "{UserID}")
            self.assertEqual(out["EMailAddress"], "{EMAIL}")
            self.assertEqual(out["note"], "plain text stays")
            self.assertEqual(len(out["rows"]), 2)
            self.assertEqual(out["rows"][0], {"Company": "{COMPANY_ID}", "Plant": "{PLANT_ID}", "keep": 1})
            self.assertEqual(out["rows"][1], {"Company": "{COMPANY_ID}", "Plant": "{PLANT_ID}", "keep": 2})

    @staticmethod
    def _call(method, idx):
        return {"name": f"{method}-{idx}", "timestamp": "t", "children": [],
                "BOCall": {"id": str(idx), "request": {"method": "POST", "body": {}, "headers": {}},
                           "response": {"body": {}}}}

    def _tree(self):
        metafx = "{INSTANCE}/api/v2/odata/{COMPANY_ID}/Ice.LIB.MetaFXSvc"
        other = "{INSTANCE}/api/v2/odata/{COMPANY_ID}/Ice.BO.UserFileSvc"
        # No prevSibling/nextSibling keys anywhere, as in many real traces.
        return {"name": "root", "children": [
            {"name": metafx, "children": [self._call("GetApp", 1), self._call("ImportLayers", 2)]},
            {"name": other, "children": [self._call("GetRows", 3)]},
        ]}

    def test_sensitive_values_mode_does_not_add_sibling_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            src = self._write_input(temp_dir, self._tree())
            ok, msg, _ = self._run_process(src, temp_dir / "reduced", temp_dir / "delta", redact_mode="sensitive-values")
            self.assertTrue(ok, msg)
            self.assertIn("structure=identical", msg)
            out = (temp_dir / "reduced" / "trace_reduced.json").read_text(encoding="utf-8")
            self.assertNotIn("Sibling", out)

    def _trim_tree(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            src = self._write_input(temp_dir, self._tree())
            ok, msg, _ = trim_trace_paths.process_file(
                src=src, drop_patterns=[], drop_keys=set(), in_place=False, out_dir=str(temp_dir / "reduced"),
                suffix="_reduced", pretty=True, dry_run=False, do_prune_empty=False, show_samples=0,
                show_top_rules=0, no_backup=True, drop_node_name_patterns=[],
                drop_parent_name_patterns=list(trim_trace_paths.DEFAULT_DROP_PARENT_NAME_PATTERNS),
                strip_empty_call_context=False, show_call_summary=False, sequence_only=False,
                redact_mode="aggressive", call_context_policy="purge", light_repair_json=True, sample_size=0,
                sample_include_patterns=[], sample_exclude_patterns=[], delta_mode="off", delta_format="jsonc",
                delta_out_dir=str(temp_dir / "delta"), delta_max_value_len=200, delta_redact_values=True, **kwargs)
            self.assertTrue(ok, msg)
            return (temp_dir / "reduced" / "trace_reduced.json").read_text(encoding="utf-8")

    def test_trim_keeps_import_layers_under_dropped_service(self):
        out = self._trim_tree()
        self.assertIn("ImportLayers-2", out)
        self.assertNotIn("GetApp-1", out)  # the service's metadata noise is still dropped
        self.assertIn("GetRows-3", out)

    def test_trim_drops_whole_service_when_no_keep_pattern_matches(self):
        out = self._trim_tree(keep_node_name_patterns=[])
        self.assertNotIn("MetaFXSvc", out)
        self.assertNotIn("ImportLayers-2", out)

    def test_sensitive_values_mode_keeps_empty_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            src = self._write_input(
                temp_dir,
                {
                    "empty": "",
                    "nested": {"empty": "", "User": "fakeuser"},
                },
            )
            out_dir = temp_dir / "reduced"
            delta_dir = temp_dir / "delta"

            ok, msg, detail = self._run_process(
                src,
                out_dir,
                delta_dir,
                redact_mode="sensitive-values",
            )

            self.assertTrue(ok, msg)
            self.assertIn("structure=identical", msg)
            out = json.loads((out_dir / "trace_reduced.json").read_text(encoding="utf-8"))
            self.assertEqual(out["empty"], "")
            self.assertEqual(out["nested"]["empty"], "")
            self.assertEqual(out["nested"]["User"], "{USERNAME}")

    def test_sensitive_values_mode_with_sample_size_skips_structure_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_dir = Path(tmp)
            src = self._write_input(
                temp_dir,
                {
                    "rows": [{"Company": "ZZTEST", "keep": i} for i in range(5)],
                },
            )
            out_dir = temp_dir / "reduced"
            delta_dir = temp_dir / "delta"

            ok, msg, detail = trim_trace_paths.process_file(
                src=src,
                drop_patterns=[],
                drop_keys=set(),
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
                redact_mode="sensitive-values",
                call_context_policy="purge",
                light_repair_json=True,
                sample_size=2,
                sample_include_patterns=[],
                sample_exclude_patterns=[],
                delta_mode="off",
                delta_format="jsonc",
                delta_out_dir=str(delta_dir),
                delta_max_value_len=200,
                delta_redact_values=True,
            )

            self.assertTrue(ok, msg)
            self.assertIn("structure-check=skipped (sampling trims structure)", msg)
            out = json.loads((out_dir / "trace_reduced.json").read_text(encoding="utf-8"))
            self.assertEqual(len(out["rows"]), 2)


class TestModeMatrix(unittest.TestCase):
    def test_mode_choices_and_default(self):
        with patch("sys.argv", ["trim_trace_paths.py", "trace.json"]):
            args = trim_trace_paths.parse_args()

        self.assertEqual(args.mode, "trim")
        self.assertEqual(
            sorted(trim_trace_paths.MODE_MATRIX.keys()),
            ["redact-inplace", "redact-only", "trim", "trim-context"],
        )

    def _parse(self, *argv):
        err = io.StringIO()
        with patch("sys.argv", ["trim_trace_paths.py", "trace.json", *argv]), redirect_stderr(err):
            args = trim_trace_paths.parse_args()
        return args, err.getvalue()

    def test_deprecated_redact_mode_maps_to_mode_and_warns(self):
        """0.2.0.0a8 scripts keep working: --redact-mode picks the mode with the same redaction profile."""
        for value, mode, profile in (("aggressive", "trim", "aggressive"), ("context-only", "trim-context", "context-only")):
            args, err = self._parse("--redact-mode", value)
            self.assertEqual(args.mode, mode)
            self.assertIn("deprecated", err)
            self.assertIn(f"--mode {mode}", err)
            self.assertFalse(hasattr(args, "redact_mode"))
            _, redact_mode = trim_trace_paths.apply_mode_defaults(args)
            self.assertEqual(redact_mode, profile)

    def test_deprecated_redact_mode_with_matching_mode_is_fine(self):
        args, _ = self._parse("--mode", "trim-context", "--redact-mode", "context-only")
        self.assertEqual(args.mode, "trim-context")

    def test_deprecated_redact_mode_conflicting_with_mode_is_an_error(self):
        with self.assertRaises(SystemExit):
            self._parse("--mode", "redact-only", "--redact-mode", "aggressive")

    def test_no_warning_without_redact_mode(self):
        args, err = self._parse("--mode", "trim-context")
        self.assertEqual((args.mode, err), ("trim-context", ""))
        self.assertIn("--mode", args._explicit_flags)

    def test_redact_inplace_bundle_applies_defaults(self):
        import argparse

        args = argparse.Namespace(mode="redact-inplace", in_place=False, no_backup=False, pretty=False, out_dir="reduced")
        args, redact_mode = trim_trace_paths.apply_mode_defaults(args)

        self.assertEqual(redact_mode, "sensitive-values")
        self.assertTrue(args.in_place)
        self.assertTrue(args.no_backup)
        self.assertTrue(args.pretty)

    def test_redact_only_bundle_sets_out_dir(self):
        import argparse

        args = argparse.Namespace(mode="redact-only", in_place=False, no_backup=False, pretty=False, out_dir="reduced")
        args, redact_mode = trim_trace_paths.apply_mode_defaults(args)

        self.assertEqual(redact_mode, "sensitive-values")
        self.assertEqual(args.out_dir, "redacted")
        self.assertFalse(args.in_place)

    def test_explicit_flags_win_over_mode_defaults(self):
        import argparse

        args = argparse.Namespace(
            mode="redact-inplace",
            in_place=False,
            no_backup=False,
            pretty=False,
            out_dir="reduced",
            _explicit_flags={"--out-dir"},
        )
        args, redact_mode = trim_trace_paths.apply_mode_defaults(args)

        self.assertEqual(redact_mode, "sensitive-values")
        self.assertTrue(args.in_place)
        self.assertTrue(args.pretty)
        self.assertEqual(args.out_dir, "reduced")

    def test_trim_modes_map_to_redact_profiles(self):
        import argparse

        for mode, expected in (("trim", "aggressive"), ("trim-context", "context-only")):
            args = argparse.Namespace(mode=mode, in_place=False, no_backup=False, pretty=False, out_dir="reduced")
            args, redact_mode = trim_trace_paths.apply_mode_defaults(args)

            self.assertEqual(redact_mode, expected)

    def test_explicit_flags_tracked_by_parse_args(self):
        with patch("sys.argv", ["trim_trace_paths.py", "trace.json", "--mode", "redact-only", "--out-dir", "custom"]):
            args = trim_trace_paths.parse_args()

        self.assertEqual(args.mode, "redact-only")
        self.assertIn("--out-dir", args._explicit_flags)
        self.assertNotIn("--in-place", args._explicit_flags)

        args, redact_mode = trim_trace_paths.apply_mode_defaults(args)
        self.assertEqual(redact_mode, "sensitive-values")
        self.assertEqual(args.out_dir, "custom")


if __name__ == "__main__":
    unittest.main()