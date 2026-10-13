"""Enforce module-level test coverage inventory.

This test ensures no Python module in key code roots is left without at least
one automated smoke test path.
"""

import importlib
import py_compile
import unittest
from pathlib import Path


PACKAGE_MODULE_MATRIX = {
    "__init__",
    "__main__",
    "access_scope",
    "artifact_validation",
    "analysis",
    "auth",
    "baq",
    "base_client",
    "boreader",
    "crypto",
    "efx",
    "efx_library",
    "export_all",
    "file_service",
    "find_sensitive_data",
    "fs_ops",
    "KineticCore",
    "metafx",
    "repo_maker",
    "repo_maker_core",
    "report_service",
    "repo_context",
    "solutions",
    "tax_service",
    "trim_trace_paths",
    "zdatatable",
}

SCRIPT_MODULE_MATRIX = {
    "apply_branch_protection.py",
    "build_kinetic_app.py",
    "build_kinetic_custom_layer.py",
    "build_kinetic_reports.py",
    "env_init.py",
    "extract_and_format_content.py",
    "find_sensitive_data.py",
    "generate-commit.py",
    "git_stash_review.py",
    "json_helper.py",
    "kinetic_devops_utils.py",
    "merge_text_files.py",
    "proj_init.py",
    "pull_api_store.py",
    "pull_tax_configs.py",
    "refresh_post_db.py",
    "repo_maker.py",
    "rename_MetaUis.py",
    "stash_dangling_commit.py",
    "stash_dangling_objects.py",
    "tax_clear.py",
}


class TestModuleCoverageMatrix(unittest.TestCase):
    """Guardrail to ensure no key module is left without a test path."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]

    def test_package_matrix_matches_repo_modules(self):
        package_dir = self.repo_root / "kinetic_devops"
        discovered = {
            path.stem
            for path in package_dir.glob("*.py")
            if path.is_file()
        }

        self.assertEqual(
            PACKAGE_MODULE_MATRIX,
            discovered,
            msg=(
                "Package module matrix is out of sync. "
                "Update PACKAGE_MODULE_MATRIX for any added/removed module."
            ),
        )

    def test_script_matrix_matches_repo_modules(self):
        scripts_dir = self.repo_root / "scripts"
        discovered = {
            path.name
            for path in scripts_dir.glob("*.py")
            if path.is_file()
        }

        self.assertEqual(
            SCRIPT_MODULE_MATRIX,
            discovered,
            msg=(
                "Script module matrix is out of sync. "
                "Update SCRIPT_MODULE_MATRIX for any added/removed script module."
            ),
        )

    def test_import_smoke_for_all_package_modules(self):
        for module_name in sorted(PACKAGE_MODULE_MATRIX):
            with self.subTest(module=module_name):
                importlib.import_module(f"kinetic_devops.{module_name}")

    def test_compile_smoke_for_all_script_modules(self):
        scripts_dir = self.repo_root / "scripts"
        for script_name in sorted(SCRIPT_MODULE_MATRIX):
            with self.subTest(script=script_name):
                py_compile.compile(str(scripts_dir / script_name), doraise=True)


if __name__ == "__main__":
    unittest.main()
