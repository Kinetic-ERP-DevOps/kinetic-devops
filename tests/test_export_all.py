"""Unit tests for ExportAllTheThings core export orchestration helpers."""

import unittest

from kinetic_devops.export_all import (
    _collect_function_ids_from_payload,
    _extract_file_payload,
    _parse_query_params,
    _split_companies,
    _deep_template,
)


class TestExportAllParsing(unittest.TestCase):
    def test_collect_function_ids_from_nested_payload(self):
        payload = {
            "returnObj": {
                "EfxLibrary": [{"LibraryID": "ExportAllTheThings"}],
                "EfxFunction": [
                    {"FunctionID": "ExportAllCustomBAQs"},
                    {"FunctionID": "ExportAllFunctionLibraries"},
                    {"FunctionID": "GetApp"},
                ],
            }
        }

        function_ids = _collect_function_ids_from_payload(payload)
        self.assertIn("ExportAllCustomBAQs", function_ids)
        self.assertIn("ExportAllFunctionLibraries", function_ids)
        self.assertIn("GetApp", function_ids)

    def test_extract_file_payload_prefers_zipbase64(self):
        # base64("hello") = aGVsbG8=
        result = {
            "Success": True,
            "ZipBase64": "aGVsbG8=",
            "Content": "fallback text",
        }

        payload_key, payload_type, payload_bytes, payload_text = _extract_file_payload(result)
        self.assertEqual(payload_key, "ZipBase64")
        self.assertEqual(payload_type, "base64")
        self.assertEqual(payload_bytes, b"hello")
        self.assertIsNone(payload_text)

    def test_extract_file_payload_text_fallback(self):
        result = {
            "Success": True,
            "returnObj": {
                "Content": "plain text content"
            },
        }

        payload_key, payload_type, payload_bytes, payload_text = _extract_file_payload(result)
        self.assertEqual(payload_key, "Content")
        self.assertEqual(payload_type, "text")
        self.assertIsNone(payload_bytes)
        self.assertEqual(payload_text, "plain text content")

    def test_parse_query_params(self):
        parsed = _parse_query_params("$top=100&$skip=200&name=test")
        self.assertEqual(parsed.get("$top"), "100")
        self.assertEqual(parsed.get("$skip"), "200")
        self.assertEqual(parsed.get("name"), "test")

    def test_deep_template(self):
        source = {
            "endpoint": "/api/v2/odata/{company}/Ice.BO.BAQDesignerSvc/GetList",
            "nested": {
                "url": "{base_url}/api/v2/odata/{company}/BaqSvc/zTest/Data"
            },
            "list": ["{company}", "x"],
        }
        rendered = _deep_template(source, {"company": "EPIC06", "base_url": "https://kinetic.example.com"})
        self.assertEqual(rendered["endpoint"], "/api/v2/odata/EPIC06/Ice.BO.BAQDesignerSvc/GetList")
        self.assertEqual(rendered["nested"]["url"], "https://kinetic.example.com/api/v2/odata/EPIC06/BaqSvc/zTest/Data")
        self.assertEqual(rendered["list"][0], "EPIC06")

    def test_split_companies_preserves_order_and_dedups_case_insensitive(self):
        parsed = _split_companies("  A01 , b02,A01, B02 , C03 ,, ")
        self.assertEqual(parsed, ["A01", "b02", "C03"])


if __name__ == "__main__":
    unittest.main()
