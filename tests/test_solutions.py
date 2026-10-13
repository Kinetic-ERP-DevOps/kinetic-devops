"""Unit tests for Solution Workbench backup/recreate helpers."""

import json
import os
import tempfile
import types
import unittest
import zipfile

from kinetic_devops.solutions import (
    _apply_install_flags,
    _build_high_vis_summary,
    _classify_install_message,
    _extract_named_messages,
    _collect_text_findings,
    _extract_layer_conflicts,
    _solution_registration_signature,
    _extract_solution_table_names,
    _extract_dynamic_rows_payload,
    _resolve_hydrate_tables,
    _sanitize_tableset_for_recreate,
    _sanitize_dynamic_payload_for_add,
    KineticSolutionService,
)


class TestSolutionHelpers(unittest.TestCase):
    def test_solution_registration_signature_is_logical(self):
        tableset = {
            "EPSolutionDetail": [
                {"TableName": "Menu", "SolutionTypeID": "Menu", "Selected": 1, "Key1": None},
                {"TableName": "BpDirective", "SolutionTypeID": "BpDirective", "Selected": 4, "Key1": ""},
            ],
            "EPSolutionPackage": [
                {"TableName": "Ice.Menu", "Key1": ""},
                {"TableName": "MetaUI", "Key1": "Erp.UI.NonConfEntry~Layer~KNTCCustLayer"},
            ],
        }
        sig = _solution_registration_signature(tableset)
        self.assertEqual(len(sig["detail"]), 2)
        self.assertEqual(len(sig["package"]), 2)
        self.assertIn("Menu|Menu|1|", sig["detail"])
        self.assertIn("MetaUI|Erp.UI.NonConfEntry~Layer~KNTCCustLayer", sig["package"])

    def test_extract_layer_conflicts_identifier_format(self):
        messages = [
            "Import cancelled or failed for 'Erp.UI.NonConfEntry~2026-03-27-NonConfEntry-FTT~KNTCCustLayer'.",
            "Layer with identifier Erp.UI.NonConfEntry~2026-03-27-NonConfEntry-FTT~KNTCCustLayer already exists in another company",
        ]
        conflicts = _extract_layer_conflicts(messages)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["application_id"], "Erp.UI.NonConfEntry")
        self.assertEqual(conflicts[0]["layer_name"], "2026-03-27-NonConfEntry-FTT")
        self.assertEqual(conflicts[0]["layer_type"], "KNTCCustLayer")

    def test_extract_layer_conflicts_alt_warning_format(self):
        messages = [
            "Cannot override all companies customization for application layer 2023-10_AssignedDept Erp.UI.NonConfEntry as the layer already exists in another company.",
            "Cannot override all companies customization for application layer 2026-03-27-NonConfEntry-FTT Erp.UI.NonConfEntry as the layer already exists in another company.",
        ]
        conflicts = _extract_layer_conflicts(messages)
        self.assertEqual(len(conflicts), 2)
        self.assertEqual(conflicts[0]["layer_type"], "KNTCCustLayer")
        self.assertEqual(conflicts[0]["application_id"], "Erp.UI.NonConfEntry")
        self.assertEqual(conflicts[0]["layer_name"], "2023-10_AssignedDept")

    def test_extract_solution_table_names_skips_undefined(self):
        tableset = {
            "EPSolutionDetail": [
                {"TableName": "QueryHdr"},
                {"TableName": "undefined"},
                {"TableName": " Menu "},
            ],
            "EPSolutionPackage": [
                {"TableName": "XXXDef"},
                {"TableName": ""},
            ],
        }

        result = _extract_solution_table_names(tableset)
        self.assertEqual(result, ["Menu", "QueryHdr", "XXXDef"])

    def test_sanitize_tableset_replaces_ids_and_system_fields(self):
        source = {
            "ExportPackage": [
                {
                    "PackageID": "SRC_SOL",
                    "SolutionID": "SRC_SOL",
                    "Description": "Source",
                    "SysRowID": "abc",
                    "SysRevID": 1,
                }
            ],
            "EPSolutionDetail": [
                {
                    "SolutionID": "SRC_SOL",
                    "TableName": "QueryHdr",
                    "Key1": "MyBAQ",
                    "ForeignSysRowID": "foreign-id",
                    "BitFlag": 0,
                }
            ],
            "EPHistory": [
                {"SolutionID": "SRC_SOL", "Something": "skip"}
            ],
        }

        result = _sanitize_tableset_for_recreate(source, "SRC_SOL", "TGT_SOL")

        self.assertEqual(result["ExportPackage"][0]["PackageID"], "TGT_SOL")
        self.assertEqual(result["ExportPackage"][0]["SolutionID"], "TGT_SOL")
        self.assertNotIn("SysRowID", result["ExportPackage"][0])
        self.assertNotIn("SysRevID", result["ExportPackage"][0])
        self.assertEqual(result["ExportPackage"][0]["RowMod"], "A")

        self.assertEqual(result["EPSolutionDetail"][0]["SolutionID"], "TGT_SOL")
        self.assertEqual(result["EPSolutionDetail"][0]["ForeignSysRowID"], "foreign-id")
        self.assertEqual(result["EPHistory"], [])

    def test_resolve_hydrate_tables_handles_ice_prefix(self):
        available = ["QueryHdr", "Ice.Menu", "XXXDef", "BpDirectiveGroup"]
        requested = ["menu", "bpdirectivegroup", "MetaUI"]
        result = _resolve_hydrate_tables(available, requested)
        self.assertEqual(result, ["BpDirectiveGroup", "Ice.Menu"])

    def test_extract_and_sanitize_dynamic_payload(self):
        raw = {
            "returnObj": {
                "DynamicTable": [
                    {
                        "SolutionID": "SRC_SOL",
                        "TableName": "Menu",
                        "Key1": "Main",
                        "SysRowID": "x",
                        "SysRevID": 9,
                    }
                ]
            }
        }

        extracted = _extract_dynamic_rows_payload(raw)
        self.assertIn("DynamicTable", extracted)
        sanitized = _sanitize_dynamic_payload_for_add(extracted, "SRC_SOL", "TGT_SOL")
        self.assertEqual(sanitized["DynamicTable"][0]["SolutionID"], "TGT_SOL")
        self.assertEqual(sanitized["DynamicTable"][0]["RowMod"], "A")
        self.assertNotIn("SysRowID", sanitized["DynamicTable"][0])

    def test_collect_text_findings_ignores_zero_errors(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "Validation.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("0 errors found\n")
                f.write("WARNING: something optional\n")
                f.write("Fatal: bad state\n")
            findings = _collect_text_findings(path)
            severities = [item["severity"] for item in findings]
            self.assertIn("warning", severities)
            self.assertIn("error", severities)

    def test_validate_build_artifacts_pre_install_ready(self):
        with tempfile.TemporaryDirectory() as td:
            package = os.path.join(td, "pkg.zip")
            with zipfile.ZipFile(package, "w") as zf:
                zf.writestr("manifest.xml", "<m />")

            log_path = os.path.join(td, "pkg.zip_Build.log")
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("Build complete\n")

            svc = object.__new__(KineticSolutionService)
            summary = svc.validate_build_artifacts(package, build_log_file=log_path, validation_file="")
            self.assertTrue(summary["pre_install_ready"])

            validation_path = os.path.join(td, "Validation.txt")
            with open(validation_path, "w", encoding="utf-8") as f:
                f.write("ERROR: failure happened\n")
            summary2 = svc.validate_build_artifacts(package, build_log_file=log_path, validation_file=validation_path)
            self.assertFalse(summary2["pre_install_ready"])

    def test_extract_named_messages_and_classification(self):
        payload = {
            "returnObj": {
                "Message": "Import canceled. Record already exists.",
                "Inner": [
                    {"validationMsg": "Changes may need Regenerate Data Model."},
                    {"Message": "A hard error happened."},
                ],
            }
        }
        msgs = _extract_named_messages(payload)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(_classify_install_message(msgs[0]), "warning")
        self.assertEqual(_classify_install_message(msgs[1]), "warning")
        self.assertEqual(_classify_install_message(msgs[2]), "failure")

    def test_build_high_vis_summary(self):
        payload = {
            "Message": "Import canceled. Record already exists.",
            "logRecords": "ERROR: failed to load item",
        }
        summary = _build_high_vis_summary(payload)
        self.assertEqual(summary["count"], 2)
        self.assertEqual(len(summary["warnings"]), 1)
        self.assertEqual(len(summary["failures"]), 1)

    def test_apply_install_flags_replace_profile(self):
        settings = {
            "MainInstallSettings": [
                {
                    "AutoOverwriteDuplicateFile": False,
                    "AutoOverwriteDuplicateData": False,
                    "DeletePreviousInstall": False,
                    "OverrideDirectives": False,
                }
            ]
        }
        updated, flags = _apply_install_flags(settings, replace=True)
        row = updated["MainInstallSettings"][0]
        self.assertTrue(row["AutoOverwriteDuplicateFile"])
        self.assertTrue(row["AutoOverwriteDuplicateData"])
        self.assertTrue(row["DeletePreviousInstall"])
        self.assertTrue(row["OverrideDirectives"])
        self.assertTrue(flags["replace"])

    def test_recreate_auto_hydrates_when_membership_empty(self):
        with tempfile.TemporaryDirectory() as td:
            backup_path = os.path.join(td, "backup.json")
            backup = {
                "metadata": {
                    "solution_id": "SRC_SOL",
                    "table_names": ["Menu"],
                },
                "tableset": {
                    "ExportPackage": [
                        {"PackageID": "SRC_SOL", "SolutionID": "SRC_SOL"}
                    ],
                    "EPSolutionDetail": [
                        {"SolutionID": "SRC_SOL", "TableName": "Menu", "Key1": "MainMenu"}
                    ],
                    "EPSolutionPackage": [],
                },
                "dynamic_items": {
                    "Menu": {
                        "returnObj": {
                            "DynamicTable": [
                                {"SolutionID": "SRC_SOL", "TableName": "Menu", "Key1": "MainMenu"}
                            ]
                        }
                    }
                },
                "tracked_items": {},
            }
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(backup, f)

            class StubService:
                def __init__(self):
                    self.config = {"company": "ACME"}
                    self.hydrated = False

                def solution_exists(self, solution_id, company=""):
                    return False

                def get_new_export_package(self, template_tableset=None, company=""):
                    return {"ExportPackage": [], "EPSolutionDetail": [], "EPSolutionPackage": []}

                def update_solution_tableset(self, ds, company=""):
                    return ds

                def get_solution(self, solution_id, company=""):
                    if self.hydrated:
                        return {
                            "returnObj": {
                                "EPSolutionDetail": [
                                    {"SolutionID": solution_id, "TableName": "Menu", "Key1": "MainMenu"}
                                ],
                                "EPSolutionPackage": [],
                            }
                        }
                    return {
                        "returnObj": {
                            "EPSolutionDetail": [],
                            "EPSolutionPackage": [],
                        }
                    }

                def add_items_to_solution_and_save(self, solution_id, ds_to_add, records_to_delete, company=""):
                    self.hydrated = True
                    return {"success": True}

                def _build_selected_solution_rows_from_dynamic(self, source_dynamic, company=""):
                    return []

                def _call(self, method_name, payload, company=""):
                    return {"returnObj": []}

            stub = StubService()
            stub._hydrate_solution_membership_from_backup = types.MethodType(
                KineticSolutionService._hydrate_solution_membership_from_backup,
                stub,
            )
            result = KineticSolutionService.recreate_solution_from_backup(stub, backup_path)

            self.assertTrue(result["success"])
            self.assertTrue(result["auto_hydrate_attempted"])
            self.assertEqual(result["target_membership"]["detail"], 1)
            self.assertIn("Menu", result["hydrated_tables"])
            self.assertTrue(result["target_matches_source_signature"])

    def test_recreate_raises_when_target_signature_mismatches_source(self):
        with tempfile.TemporaryDirectory() as td:
            backup_path = os.path.join(td, "backup.json")
            backup = {
                "metadata": {
                    "solution_id": "SRC_SOL",
                    "table_names": ["Menu"],
                },
                "tableset": {
                    "ExportPackage": [
                        {"PackageID": "SRC_SOL", "SolutionID": "SRC_SOL"}
                    ],
                    "EPSolutionDetail": [
                        {"SolutionID": "SRC_SOL", "TableName": "Menu", "Key1": "MainMenu", "Selected": 1, "SolutionTypeID": "Menu"}
                    ],
                    "EPSolutionPackage": [
                        {"SolutionID": "SRC_SOL", "TableName": "Ice.Menu", "Key1": ""}
                    ],
                },
                "dynamic_items": {},
                "tracked_items": {},
            }
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(backup, f)

            class StubService:
                def __init__(self):
                    self.config = {"company": "ACME"}

                def solution_exists(self, solution_id, company=""):
                    return False

                def get_new_export_package(self, template_tableset=None, company=""):
                    return {"ExportPackage": [], "EPSolutionDetail": [], "EPSolutionPackage": []}

                def update_solution_tableset(self, ds, company=""):
                    return ds

                def get_solution(self, solution_id, company=""):
                    return {
                        "returnObj": {
                            "EPSolutionDetail": [
                                {"SolutionID": solution_id, "TableName": "Menu", "Key1": "DifferentMenu", "Selected": 1, "SolutionTypeID": "Menu"}
                            ],
                            "EPSolutionPackage": [
                                {"SolutionID": solution_id, "TableName": "Ice.Menu", "Key1": "DIFF"}
                            ],
                        }
                    }

            stub = StubService()
            with self.assertRaises(RuntimeError) as exc:
                KineticSolutionService.recreate_solution_from_backup(stub, backup_path)
            self.assertIn("does not match source intent", str(exc.exception))

    def test_install_fails_by_default_on_metafx_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            cab_file = os.path.join(td, "pkg.zip")
            with zipfile.ZipFile(cab_file, "w") as zf:
                zf.writestr("manifest.xml", "<m />")

            class StubService:
                def file_to_base64(self, _path):
                    return "ZmFrZQ=="

                def get_install_settings(self, _cab_data, _solution_file, company=""):
                    return {
                        "MainInstallSettings": [
                            {
                                "AutoOverwriteDuplicateFile": False,
                                "AutoOverwriteDuplicateData": False,
                                "DeletePreviousInstall": False,
                                "OverrideDirectives": False,
                            }
                        ]
                    }

                def _call(self, method_name, payload, company=""):
                    if method_name != "Install":
                        raise AssertionError(f"Unexpected method: {method_name}")
                    return {
                        "returnObj": {
                            "Message": (
                                "Cannot override all companies customization for application layer "
                                "2026-03-27-NonConfEntry-FTT Erp.UI.NonConfEntry as the layer already exists in another company."
                            )
                        }
                    }

            stub = StubService()

            with self.assertRaises(RuntimeError) as exc:
                KineticSolutionService.install_solution_cab(
                    stub,
                    cab_file,
                    skip_preflight=True,
                    auto_clear_layer_conflicts=False,
                )

            self.assertIn("--auto-clear-layer-conflicts", str(exc.exception))

    def test_remove_solution_removes_membership(self):
        class StubService:
            def __init__(self):
                self.config = {"company": "ACME"}
                self.exists = True

            def solution_exists(self, solution_id, company=""):
                return self.exists

            def get_solution(self, solution_id, company=""):
                return {
                    "returnObj": {
                        "EPSolutionDetail": [{"TableName": "Menu"}, {"TableName": "MetaUI"}],
                        "EPSolutionPackage": [{"TableName": "ExportPackage"}],
                    }
                }

            def delete_solution(self, solution_id, company=""):
                self.exists = False

        stub = StubService()
        result = KineticSolutionService.remove_solution(stub, "NonConf-FTT")

        self.assertTrue(result["success"])
        self.assertTrue(result["exists_before"])
        self.assertFalse(result["exists_after"])
        self.assertEqual(result["removed_membership"]["detail"], 2)
        self.assertEqual(result["removed_membership"]["package"], 1)


if __name__ == "__main__":
    unittest.main()