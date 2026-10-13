"""Service surface smoke tests migrated from root-level test_services.py."""

import subprocess
import sys
import unittest

from tests.cli_matrix import scaled_timeout

from kinetic_devops import (
    KineticBAQService,
    KineticBOReaderService,
    KineticFileService,
)


class TestServiceSurface(unittest.TestCase):
    """Validate service class availability and basic public surface."""

    def test_service_classes_are_importable(self):
        self.assertTrue(callable(KineticBAQService))
        self.assertTrue(callable(KineticBOReaderService))
        self.assertTrue(callable(KineticFileService))

    def test_public_methods_exist(self):
        services = [
            KineticBAQService,
            KineticBOReaderService,
            KineticFileService,
        ]

        for service_class in services:
            methods = [
                name
                for name in dir(service_class)
                if not name.startswith("_") and callable(getattr(service_class, name))
            ]
            self.assertGreater(len(methods), 0, f"No public methods detected on {service_class.__name__}")


class TestServiceCliSmoke(unittest.TestCase):
    """Verify each service CLI responds to --help without crashing."""

    def _assert_help_works(self, module_name: str):
        result = subprocess.run(
            [sys.executable, "-m", module_name, "--help"],
            capture_output=True,
            text=True,
            timeout=scaled_timeout(5),
        )
        self.assertEqual(result.returncode, 0, f"{module_name} --help failed")
        self.assertIn("usage:", result.stdout.lower(), f"{module_name} help output missing usage")

    def test_baq_cli_help(self):
        self._assert_help_works("kinetic_devops.baq")

    def test_boreader_cli_help(self):
        self._assert_help_works("kinetic_devops.boreader")

    def test_file_service_cli_help(self):
        self._assert_help_works("kinetic_devops.file_service")


if __name__ == "__main__":
    unittest.main()
