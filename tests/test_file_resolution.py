"""Tests for KineticBaseClient file conflict resolution behavior."""

import os
import tempfile
import unittest
from unittest.mock import patch

from kinetic_devops.base_client import KineticBaseClient


class TestFileResolution(unittest.TestCase):
    def _make_client_stub(self) -> KineticBaseClient:
        client = object.__new__(KineticBaseClient)
        client._file_conflict_resolution = "timestamp"
        client._file_force = False
        client._file_force_low = False
        client._file_force_medium = False
        client._file_force_high = False
        client._file_force_critical = False
        client._file_no_force_low = False
        client._file_no_force_none = False
        client._file_confirm_overwrite = True
        client._file_warn_on_drift = True
        return client

    def test_timestamp_creates_alternate_name_for_existing_file(self):
        client = self._make_client_stub()
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "artifact.zip")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x")

            resolved = client.resolve_output_path(target, conflict_resolution="timestamp", warn_on_drift=False)
            self.assertNotEqual(resolved, target)
            self.assertTrue(resolved.endswith(".zip"))
            self.assertIn("artifact_", os.path.basename(resolved))

    def test_overwrite_blocks_without_force_for_critical_risk(self):
        client = self._make_client_stub()
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "artifact.zip")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x")

            risk = {
                "risk_level": "critical",
                "system_action": "block",
                "reason": "untracked",
                "tracked": False,
            }
            with patch("kinetic_devops.base_client.describe_overwrite_risk", return_value=risk):
                with self.assertRaises(PermissionError):
                    client.resolve_output_path(target, conflict_resolution="overwrite", force=False, confirm_overwrite=False)

    def test_overwrite_requires_force_high_for_high_risk(self):
        client = self._make_client_stub()
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "artifact.zip")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x")

            risk = {
                "risk_level": "high",
                "system_action": "block",
                "reason": "ignored_path",
                "tracked": False,
            }
            with patch("kinetic_devops.base_client.describe_overwrite_risk", return_value=risk):
                with self.assertRaises(PermissionError):
                    client.resolve_output_path(
                        target,
                        conflict_resolution="overwrite",
                        force=False,
                        confirm_overwrite=False,
                        warn_on_drift=False,
                    )

            client._file_force_high = True
            with patch("kinetic_devops.base_client.describe_overwrite_risk", return_value=risk):
                resolved = client.resolve_output_path(target, conflict_resolution="overwrite", confirm_overwrite=False, warn_on_drift=False)
            self.assertEqual(resolved, target)

    def test_overwrite_requires_force_medium_for_medium_risk(self):
        client = self._make_client_stub()
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "artifact.zip")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x")

            risk = {
                "risk_level": "medium",
                "system_action": "block",
                "reason": "ignored_ext",
                "tracked": False,
            }
            with patch("kinetic_devops.base_client.describe_overwrite_risk", return_value=risk):
                with self.assertRaises(PermissionError):
                    client.resolve_output_path(target, conflict_resolution="overwrite", confirm_overwrite=False)

            client._file_force_medium = True
            with patch("kinetic_devops.base_client.describe_overwrite_risk", return_value=risk):
                resolved = client.resolve_output_path(target, conflict_resolution="overwrite", confirm_overwrite=False)
            self.assertEqual(resolved, target)

    def test_overwrite_requires_confirmation_in_non_interactive_mode(self):
        client = self._make_client_stub()
        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "artifact.zip")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x")

            risk = {
                "risk_level": "low",
                "system_action": "proceed",
                "reason": "tracked",
                "tracked": True,
            }
            with patch("kinetic_devops.base_client.describe_overwrite_risk", return_value=risk):
                with patch("sys.stdin.isatty", return_value=False), patch("sys.stdout.isatty", return_value=False):
                    with self.assertRaises(PermissionError):
                        client.resolve_output_path(target, conflict_resolution="overwrite", force=True, confirm_overwrite=True)


if __name__ == "__main__":
    unittest.main()
