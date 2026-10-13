"""Unit tests for ZDataTable XML parsing and drift helpers."""

import os
import tempfile
import unittest

from kinetic_devops.zdatatable import parse_zdatatable_xml, diff_field_rows


class TestZDataTableHelpers(unittest.TestCase):
    def test_parse_xml_extracts_table_and_fields(self):
        xml = """<?xml version='1.0'?>
<ZDataTableDataSet xmlns='http://www.epicor.com/Ice/300/BO/ZDataTable/ZDataTable'>
  <ZDataTable>
    <SystemCode>Erp</SystemCode>
    <DataTableID>NonConf_UD</DataTableID>
    <SchemaName>Erp</SchemaName>
    <DBTableName>NonConf_UD</DBTableName>
  </ZDataTable>
  <ZDataField>
    <FieldName>AssgnDept_c</FieldName>
    <DataType>nvarchar</DataType>
    <Required>true</Required>
    <ReadOnly>false</ReadOnly>
    <FieldFormat>x(30)</FieldFormat>
  </ZDataField>
</ZDataTableDataSet>
"""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "zdt.xml")
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
            parsed = parse_zdatatable_xml(path)

        self.assertEqual(parsed["table"]["SystemCode"], "Erp")
        self.assertEqual(parsed["table"]["DataTableID"], "NonConf_UD")
        self.assertEqual(len(parsed["fields"]), 1)
        self.assertEqual(parsed["fields"][0]["FieldName"], "AssgnDept_c")
        self.assertEqual(parsed["fields"][0]["Required"], True)

    def test_diff_field_rows_detects_schema_drift(self):
        source = {
            "FieldName": "AssgnDept_c",
            "DataType": "nvarchar",
            "Required": True,
            "FieldFormat": "x(30)",
        }
        target = {
            "FieldName": "AssgnDept_c",
            "DataType": "nvarchar",
            "Required": False,
            "FieldFormat": "x(20)",
        }
        diffs = diff_field_rows(source, target)
        self.assertIn("Required", diffs)
        self.assertIn("FieldFormat", diffs)


if __name__ == "__main__":
    unittest.main()
