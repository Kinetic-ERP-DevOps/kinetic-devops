"""Regression tests for efx_library.py's per-function C# source extraction
and push-back helpers.

Covers two bugs caught during a live round-trip test against a test environment (not
mocked here, since catching them required the real server's XAML
deserializer -- but the shapes that triggered them are reproduced as fixed
synthetic Body XML so a regression doesn't need live credentials to catch):

1. Reserializing the whole Body via ElementTree.tostring() breaks the
   server's XAML deserializer (it rewrites namespace prefixes/formatting).
   Push-back must only touch the one Code attribute via string surgery.
2. A naive `<DirectiveStep\\b` boundary regex also matches XAML
   property-element tags like `<DirectiveStep.Action>`, truncating the
   search scope to nothing and hiding the Code attribute.
"""

import unittest

from kinetic_devops.efx_library import (
    _build_new_ref_table_rows,
    _build_new_signature_rows,
    _diagnostic_is_blocking,
    _has_metadata_changes,
    _dotnet_xml_attribute_escape,
    _iter_custom_code_actions,
    _node_file_name,
    _replace_code_attribute_for_step,
)
import xml.etree.ElementTree as ET


# Trimmed but structurally real shape of a live EfxFunction.Body: single
# DirectiveStep containing one CustomCodeAction, followed by the
# DirectiveStep.VisualProperties property-element sibling that previously
# confused the step-boundary regex.
SAMPLE_BODY = (
    '<?xml version="1.0" encoding="utf-16"?>'
    '<DirectiveDefinition2 AdditionalUsings="" Version="3" '
    'xmlns="clr-namespace:Ice.Lib.Bpm.Model;assembly=Ice.Lib.Bpm.Shared" '
    'xmlns:ilbma="clr-namespace:Ice.Lib.Bpm.Model.Actions;assembly=Ice.Lib.Bpm.Shared" '
    'xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">'
    '<DirectiveDefinition2.StartNode>'
    '<DirectiveStep Next="{x:Null}" x:Name="__ReferenceID0" '
    'DisplayName="Execute Custom Code 0" Id="b25d6018-8fdd-4039-907a-ace813c17078">'
    '<DirectiveStep.Action>'
    '<ilbma:CustomCodeAction Code="var x = 1;&#xA;var y = 2;" '
    'ExecutionRule="OnceForAllAffected" Id="0" IsAsync="False" '
    'PrimaryTable="" RecordMode="Nothing" TerminateOnError="False" '
    'ValidationState="Valid" />'
    '</DirectiveStep.Action>'
    '<DirectiveStep.VisualProperties>'
    '<VisualPropertiesStorage><x:Int64 x:Key="ElementX">209</x:Int64></VisualPropertiesStorage>'
    '</DirectiveStep.VisualProperties>'
    '</DirectiveStep>'
    '</DirectiveDefinition2.StartNode>'
    '</DirectiveDefinition2>'
)


class TestIterCustomCodeActions(unittest.TestCase):
    def test_finds_the_directive_step_and_action(self):
        root = ET.fromstring(SAMPLE_BODY)
        pairs = list(_iter_custom_code_actions(root))
        self.assertEqual(len(pairs), 1)
        step, action = pairs[0]
        self.assertIsNotNone(step)
        self.assertEqual(step.get("Id"), "b25d6018-8fdd-4039-907a-ace813c17078")
        self.assertEqual(step.get("DisplayName"), "Execute Custom Code 0")
        self.assertEqual(action.get("Code"), "var x = 1;\nvar y = 2;")


class TestDotnetXmlAttributeEscape(unittest.TestCase):
    def test_escapes_match_observed_server_encoding(self):
        raw = 'if (x > 1 && y < 2) { s = "hi"; }\nnext();\t// tab'
        escaped = _dotnet_xml_attribute_escape(raw)
        self.assertNotIn("\n", escaped)
        self.assertNotIn("\t", escaped)
        self.assertIn("&gt;", escaped)
        self.assertIn("&lt;", escaped)
        self.assertIn("&amp;&amp;", escaped)
        self.assertIn("&quot;", escaped)
        self.assertIn("&#xA;", escaped)
        self.assertIn("&#x9;", escaped)


class TestReplaceCodeAttributeForStep(unittest.TestCase):
    def test_replaces_only_the_target_code_attribute(self):
        new_body = _replace_code_attribute_for_step(
            SAMPLE_BODY, "b25d6018-8fdd-4039-907a-ace813c17078", "var z = 3;"
        )
        self.assertIn('Code="var z = 3;"', new_body)
        self.assertNotIn("var x = 1;", new_body)
        # Everything else must be untouched (regression guard for the
        # ElementTree.tostring() round-trip that broke the server's XAML
        # deserializer by rewriting namespace prefixes).
        self.assertIn('xmlns:ilbma="clr-namespace:Ice.Lib.Bpm.Model.Actions;assembly=Ice.Lib.Bpm.Shared"', new_body)
        self.assertIn("<DirectiveStep.VisualProperties>", new_body)
        self.assertIn('ValidationState="Valid"', new_body)

    def test_does_not_get_confused_by_directivestep_property_elements(self):
        # Regression guard: a naive `<DirectiveStep\b` boundary match also
        # matches `<DirectiveStep.Action>` / `<DirectiveStep.VisualProperties>`
        # (XAML property-element syntax), which previously truncated the
        # search scope before reaching the Code attribute at all.
        new_body = _replace_code_attribute_for_step(
            SAMPLE_BODY, "b25d6018-8fdd-4039-907a-ace813c17078", "ok();"
        )
        self.assertIn('Code="ok();"', new_body)

    def test_unknown_step_id_raises(self):
        with self.assertRaises(ValueError):
            _replace_code_attribute_for_step(SAMPLE_BODY, "does-not-exist", "x();")

    def test_preserves_multiline_code_as_character_references(self):
        new_body = _replace_code_attribute_for_step(
            SAMPLE_BODY, "b25d6018-8fdd-4039-907a-ace813c17078", "line1();\nline2();"
        )
        self.assertIn("line1();&#xA;line2();", new_body)
        # And round-tripping it back through parsing recovers the original text.
        root = ET.fromstring(new_body)
        _, action = next(_iter_custom_code_actions(root))
        self.assertEqual(action.get("Code"), "line1();\nline2();")


class TestBuildNewSignatureRows(unittest.TestCase):
    def test_numbers_input_and_output_groups_independently(self):
        existing = [
            {"LibraryID": "L", "FunctionID": "F", "Response": False, "ParameterID": 1, "Order": 1},
            {"LibraryID": "L", "FunctionID": "F", "Response": False, "ParameterID": 2, "Order": 2},
            {"LibraryID": "L", "FunctionID": "F", "Response": True, "ParameterID": 1, "Order": 1},
        ]
        params = [
            {"argument_name": "dryRun", "data_type": "System.Boolean"},
            {"argument_name": "updatedCount", "data_type": "System.Int32", "response": True},
        ]
        rows = _build_new_signature_rows("L", "F", existing, params)

        self.assertEqual(len(rows), 2)
        dry_run_row, updated_count_row = rows
        self.assertEqual(dry_run_row["ArgumentName"], "dryRun")
        self.assertFalse(dry_run_row["Response"])
        self.assertEqual(dry_run_row["ParameterID"], 3)  # continues after existing inputs 1,2
        self.assertEqual(dry_run_row["Order"], 3)
        self.assertEqual(dry_run_row["RowMod"], "A")

        self.assertEqual(updated_count_row["ArgumentName"], "updatedCount")
        self.assertTrue(updated_count_row["Response"])
        self.assertEqual(updated_count_row["ParameterID"], 2)  # continues after existing output 1
        self.assertEqual(updated_count_row["Order"], 2)

    def test_multiple_new_params_in_same_call_number_sequentially(self):
        params = [
            {"argument_name": "a", "data_type": "System.String"},
            {"argument_name": "b", "data_type": "System.String"},
            {"argument_name": "c", "data_type": "System.String", "response": True},
        ]
        rows = _build_new_signature_rows("L", "F", [], params)
        self.assertEqual([r["ParameterID"] for r in rows], [1, 2, 1])
        self.assertEqual([r["Order"] for r in rows], [1, 2, 1])

    def test_defaults_are_sane(self):
        rows = _build_new_signature_rows("L", "F", [], [{"argument_name": "x", "data_type": "System.String"}])
        row = rows[0]
        self.assertEqual(row["LibraryID"], "L")
        self.assertEqual(row["FunctionID"], "F")
        self.assertFalse(row["Optional"])
        self.assertIsNone(row["DefaultValue"])
        self.assertEqual(row["Description"], "")
        self.assertIsNone(row["DataTypeInfo"])

    def test_empty_parameters_returns_empty_list(self):
        self.assertEqual(_build_new_signature_rows("L", "F", [], []), [])


class TestBuildNewRefTableRows(unittest.TestCase):
    def test_adds_new_table_not_already_referenced(self):
        existing = [{"LibraryID": "L", "TableID": "ERP.Part", "Updatable": False}]
        rows = _build_new_ref_table_rows("L", existing, ["ERP.Vendor"])
        self.assertEqual(rows, [{"LibraryID": "L", "TableID": "ERP.Vendor", "Updatable": False, "RowMod": "A"}])

    def test_skips_table_already_referenced(self):
        existing = [{"LibraryID": "L", "TableID": "ERP.Vendor", "Updatable": False}]
        rows = _build_new_ref_table_rows("L", existing, ["ERP.Vendor"])
        self.assertEqual(rows, [])

    def test_empty_table_ids_returns_empty_list(self):
        self.assertEqual(_build_new_ref_table_rows("L", [], []), [])


class TestNodeFileName(unittest.TestCase):
    """GUID is deliberately excluded from the filename -- see
    _node_file_name's docstring. Regression guard: a NodeId/GUID can
    legitimately differ between environments for the same logical node
    (independently created, or regenerated on import), which would
    silently break cross-environment push-back if the filename baked in
    one environment's GUID."""

    def test_uses_display_name_only_no_guid(self):
        name = _node_file_name("Execute Custom Code 0", 0, {})
        self.assertEqual(name, "Execute_Custom_Code_0.cs")

    def test_same_display_name_same_file_name_regardless_of_call(self):
        # Simulates extract (environment A) and push (environment B)
        # computing the filename independently -- must agree even though
        # a real NodeId/GUID would differ between the two environments.
        name_a = _node_file_name("Execute Custom Code 0", 0, {})
        name_b = _node_file_name("Execute Custom Code 0", 0, {})
        self.assertEqual(name_a, name_b)

    def test_empty_display_name_falls_back_to_positional(self):
        self.assertEqual(_node_file_name("", 2, {}), "CustomCode2.cs")

    def test_duplicate_display_names_get_numeric_suffix(self):
        used = {}
        first = _node_file_name("Execute Custom Code 0", 0, used)
        second = _node_file_name("Execute Custom Code 0", 1, used)
        self.assertEqual(first, "Execute_Custom_Code_0.cs")
        self.assertEqual(second, "Execute_Custom_Code_0_1.cs")


class TestHasMetadataChanges(unittest.TestCase):
    def test_true_when_value_differs(self):
        self.assertTrue(_has_metadata_changes({"Description": "old"}, {"Description": "new"}))

    def test_false_when_value_matches(self):
        self.assertFalse(_has_metadata_changes({"Description": "same"}, {"Description": "same"}))

    def test_false_when_metadata_empty(self):
        self.assertFalse(_has_metadata_changes({"Description": "same"}, {}))

    def test_true_when_any_one_of_several_keys_differs(self):
        row = {"Description": "same", "Private": False}
        self.assertTrue(_has_metadata_changes(row, {"Description": "same", "Private": True}))


class TestDiagnosticIsBlocking(unittest.TestCase):
    def test_warning_is_not_blocking(self):
        # Real diagnostic observed live: a push containing ex.GetType().Name
        # succeeded with this warning, confirming warnings don't roll back.
        msg = "MyFunction.cs(156,91): warning ECF1002: The 'System.Reflection.MemberInfo.Name' property cannot be read."
        self.assertFalse(_diagnostic_is_blocking(msg))

    def test_error_is_blocking(self):
        msg = "MyFunction.cs(10,5): error CS1002: ; expected"
        self.assertTrue(_diagnostic_is_blocking(msg))

    def test_unrecognized_format_fails_closed(self):
        self.assertTrue(_diagnostic_is_blocking("something unexpected happened"))


if __name__ == "__main__":
    unittest.main()
