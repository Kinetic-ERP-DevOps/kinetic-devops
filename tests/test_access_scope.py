"""Tests for access scope migration and validation CLI behavior."""

import argparse
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch
import zipfile

from kinetic_devops import access_scope, artifact_validation


class TestAccessScopeValidation(unittest.TestCase):
    def test_compare_scope_functional_artifacts_detects_method_gap(self):
        reference = {
            "core": [{"AccessScopeID": "MyScope", "Description": "Example scope"}],
            "entities": [{"EntityID": "ERP.BO.Company"}],
            "bo_methods": [{"EntityID": "ERP.BO.Company", "MethodID": "GetList"}],
        }
        target = {
            "core": [{"AccessScopeID": "MyScope", "Description": "Example scope"}],
            "entities": [{"EntityID": "ERP.BO.Company"}],
            "bo_methods": [],
        }

        result = artifact_validation.compare_artifact_sections(reference, target, ("core", "entities", "bo_methods"))

        self.assertTrue(result["core_identical"])
        self.assertTrue(result["entities_identical"])
        self.assertFalse(result["bo_methods_identical"])
        self.assertEqual(result["bo_methods_reference_only_count"], 1)
        self.assertEqual(result["bo_methods_target_only_count"], 0)
        self.assertFalse(result["functionally_identical"])

    def test_compare_scope_functional_artifacts_fails_closed_on_artifact_errors(self):
        reference = {
            "core_error": "403 forbidden",
            "entity_error": "",
            "bom_error": "",
            "core": [],
            "entities": [],
            "bo_methods": [],
        }
        target = {
            "core_error": "",
            "entity_error": "",
            "bom_error": "",
            "core": [],
            "entities": [],
            "bo_methods": [],
        }

        result = access_scope.compare_scope_functional_artifacts(reference, target)

        self.assertFalse(result["core_identical"])
        self.assertTrue(result["entities_identical"])
        self.assertTrue(result["bo_methods_identical"])
        self.assertFalse(result["functionally_identical"])

    def test_extract_artifact_payload_accepts_nested_pilot(self):
        payload = {
            "scope": "MyScope",
            "pilot": {
                "env": "SourceEnv",
                "core": [{"AccessScopeID": "MyScope"}],
                "entities": [{"EntityID": "ERP.BO.Company"}],
                "bo_methods": [{"MethodID": "GetList"}],
            },
        }

        result = artifact_validation.extract_artifact_payload(payload, ("core", "entities", "bo_methods"))

        self.assertEqual(result["env"], "SourceEnv")
        self.assertEqual(result["scope"], "MyScope")
        self.assertEqual(len(result["core"]), 1)

    def test_main_legacy_args_default_to_migrate(self):
        with patch("kinetic_devops.access_scope.run_scope_migration", return_value=0) as migrate, patch(
            "kinetic_devops.access_scope.run_scope_validation", return_value=0
        ) as validate:
            rc = access_scope.main(
                [
                    "--source-env",
                    "SourceEnv",
                    "--target-env",
                    "TargetEnv",
                    "--from-scope",
                    "A",
                    "--to-scope",
                    "B",
                ]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(migrate.call_count, 1)
        self.assertEqual(validate.call_count, 0)

    def test_main_validate_dispatch(self):
        with patch("kinetic_devops.access_scope.run_scope_migration", return_value=0) as migrate, patch(
            "kinetic_devops.access_scope.run_scope_validation", return_value=0
        ) as validate:
            rc = access_scope.main(
                [
                    "validate",
                    "--scope-id",
                    "MyScope",
                    "--reference-env",
                    "SourceEnv",
                    "--target-env",
                    "TargetEnv",
                ]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(migrate.call_count, 0)

    def test_main_refresh_import_dispatch(self):
        with patch("kinetic_devops.access_scope.run_scope_migration", return_value=0) as migrate, patch(
            "kinetic_devops.access_scope.run_scope_validation", return_value=0
        ) as validate, patch("kinetic_devops.access_scope.run_scope_refresh_import", return_value=0) as refresh_import:
            rc = access_scope.main(
                [
                    "refresh-import",
                    "--scope-id",
                    "MyScope",
                    "--reference-env",
                    "SourceEnv",
                    "--target-env",
                    "TargetEnv",
                    "--dry-run",
                ]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(refresh_import.call_count, 1)
        self.assertEqual(validate.call_count, 0)
        self.assertEqual(migrate.call_count, 0)

    def test_run_scope_refresh_import_restores_detached_keys_after_import_exception(self):
        original_row = {"KeyID": "key1", "Company": "EPIC06", "AccessScopeID": "MyScope"}
        current_row = {"KeyID": "key1", "Company": "EPIC06", "AccessScopeID": ""}
        target = MagicMock()
        target.config = {"nickname": "TargetEnv"}
        target.fetch_api_keys.return_value = [original_row]
        target.update_api_key_scope.side_effect = [True, True]
        target.get_api_key_by_id_and_company.return_value = current_row
        target.upload_import_file.side_effect = RuntimeError("upload failed")

        args = argparse.Namespace(
            target_env="TargetEnv",
            target_user=None,
            target_company=None,
            scope_id="MyScope",
            dry_run=False,
            pause_for_import=False,
            import_command="",
            import_eas="scope.eas",
            report="report.json",
            allow_drift=False,
            override_existing_scope=False,
            new_access_scope_id="",
        )

        with patch("kinetic_devops.access_scope.KineticAccessScopeService", return_value=target), patch(
            "kinetic_devops.access_scope._resolve_reference_scope_artifact", return_value={}
        ), patch("kinetic_devops.access_scope.build_scope_functional_artifact", return_value={}), patch(
            "kinetic_devops.access_scope.compare_scope_functional_artifacts",
            return_value={"functionally_identical": False},
        ), patch("kinetic_devops.access_scope._write_scope_refresh_report", return_value="report.json"):
            rc = access_scope.run_scope_refresh_import(args)

        self.assertEqual(rc, 1)
        self.assertEqual(
            target.update_api_key_scope.call_args_list,
            [call(original_row, ""), call(current_row, "MyScope")],
        )

    def test_run_scope_refresh_import_restores_detached_keys_after_post_import_failure(self):
        original_row = {"KeyID": "key1", "Company": "EPIC06", "AccessScopeID": "MyScope"}
        current_row = {"KeyID": "key1", "Company": "EPIC06", "AccessScopeID": ""}
        target = MagicMock()
        target.config = {"nickname": "TargetEnv"}
        target.fetch_api_keys.return_value = [original_row]
        target.update_api_key_scope.side_effect = [True, True]
        target.get_api_key_by_id_and_company.return_value = current_row
        target.upload_import_file.return_value = {"server_path": "/server/scope.eas", "relative_path": "scope.eas", "status": 200}
        target.import_access_scope_from_file.return_value = {"status": 0, "log_result": ""}
        target.access_scope_exists.return_value = False

        args = argparse.Namespace(
            target_env="TargetEnv",
            target_user=None,
            target_company=None,
            scope_id="MyScope",
            dry_run=False,
            pause_for_import=False,
            import_command="",
            import_eas="scope.eas",
            report="report.json",
            allow_drift=False,
            override_existing_scope=False,
            new_access_scope_id="",
        )

        with patch("kinetic_devops.access_scope.KineticAccessScopeService", return_value=target), patch(
            "kinetic_devops.access_scope._resolve_reference_scope_artifact", return_value={}
        ), patch("kinetic_devops.access_scope.build_scope_functional_artifact", return_value={}), patch(
            "kinetic_devops.access_scope.compare_scope_functional_artifacts",
            return_value={"functionally_identical": False},
        ), patch("kinetic_devops.access_scope._write_scope_refresh_report", return_value="report.json"):
            rc = access_scope.run_scope_refresh_import(args)

        self.assertEqual(rc, 1)
        self.assertEqual(
            target.update_api_key_scope.call_args_list,
            [call(original_row, ""), call(current_row, "MyScope")],
        )

    def test_main_validate_requires_reference_source(self):
        with self.assertRaises(SystemExit) as ctx:
            access_scope.main(
                [
                    "validate",
                    "--scope-id",
                    "MyScope",
                    "--target-env",
                    "TargetEnv",
                ]
            )

        self.assertEqual(ctx.exception.code, 2)

    def test_main_refresh_import_requires_reference_source(self):
        with self.assertRaises(SystemExit) as ctx:
            access_scope.main(
                [
                    "refresh-import",
                    "--scope-id",
                    "MyScope",
                    "--target-env",
                    "TargetEnv",
                ]
            )

        self.assertEqual(ctx.exception.code, 2)

    def test_main_export_eas_dispatch(self):
        with patch("kinetic_devops.access_scope.run_scope_migration", return_value=0) as migrate, patch(
            "kinetic_devops.access_scope.run_scope_validation", return_value=0
        ) as validate, patch("kinetic_devops.access_scope.run_scope_export_eas", return_value=0) as export_eas:
            rc = access_scope.main(
                [
                    "export-eas",
                    "--scope-id",
                    "MyScope",
                    "--source-env",
                    "SourceEnv",
                ]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(export_eas.call_count, 1)
        self.assertEqual(validate.call_count, 0)
        self.assertEqual(migrate.call_count, 0)

    def test_load_scope_artifact_from_eas_normalizes_noise_fields(self):
        tableset = {
            "AccessScope": [
                {
                    "Company": "CMP",
                    "AccessScopeID": "MyScope",
                    "Description": "Example scope",
                    "SysRevID": 123,
                    "SysRowID": "abc",
                    "RowMod": "",
                }
            ],
            "AccessScopeEntity": [
                {
                    "Company": "CMP",
                    "AccessScopeID": "MyScope",
                    "EntityType": "Service",
                    "EntityID": "ERP.BO.Company",
                    "EntityDescription": "Company Service",
                    "SysRevID": 1,
                    "SysRowID": "def",
                }
            ],
            "AccessScopeBOMethod": [
                {
                    "Company": "CMP",
                    "AccessScopeID": "MyScope",
                    "EntityType": "Service",
                    "EntityID": "ERP.BO.Company",
                    "MethodID": "GetList",
                    "SysRevID": 2,
                    "SysRowID": "ghi",
                }
            ],
        }

        with tempfile.NamedTemporaryFile(suffix=".eas", delete=False) as tmp:
            eas_path = tmp.name

        try:
            with zipfile.ZipFile(eas_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Version", "5.2.100.0")
                archive.writestr("AccessScopeTableset", json.dumps(tableset))

            artifact = artifact_validation.load_scope_artifact_from_path(eas_path)
            self.assertEqual(artifact["scope"], "MyScope")
            self.assertEqual(artifact["company"], "CMP")
            self.assertEqual(len(artifact["core"]), 1)
            self.assertEqual(len(artifact["entities"]), 1)
            self.assertEqual(len(artifact["bo_methods"]), 1)

            core_row = artifact["core"][0]
            self.assertIn("AccessScopeID", core_row)
            self.assertNotIn("Company", core_row)
            self.assertNotIn("SysRevID", core_row)
            self.assertNotIn("SysRowID", core_row)
            self.assertNotIn("RowMod", core_row)
        finally:
            if os.path.exists(eas_path):
                os.remove(eas_path)


if __name__ == "__main__":
    unittest.main()