"""Tests for overwrite risk matrix classification in fs_ops."""

import os
import shutil
import subprocess
import tempfile
import unittest

from kinetic_devops.fs_ops import describe_overwrite_risk, is_write_permitted, required_force_flag


def _has_git() -> bool:
    try:
        proc = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


@unittest.skipUnless(_has_git(), "git is required for fs_ops risk tests")
class TestFsOpsRiskMatrix(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.mkdtemp(prefix="fs_ops_test_")
        subprocess.run(["git", "init"], cwd=self.td, check=True, capture_output=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.td, ignore_errors=True)

    def test_new_path_is_none_and_proceed(self):
        path = os.path.join(self.td, "new_file.txt")
        info = describe_overwrite_risk(path)
        self.assertEqual(info["risk_level"], "none")
        self.assertEqual(info["system_action"], "proceed")

    def test_tracked_file_is_low_and_proceed(self):
        path = os.path.join(self.td, "tracked.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.td, check=True, capture_output=True)

        info = describe_overwrite_risk(path)
        self.assertTrue(info["tracked"])
        self.assertEqual(info["risk_level"], "low")
        self.assertEqual(info["system_action"], "proceed")

    def test_ignored_ext_is_medium_and_block(self):
        gi = os.path.join(self.td, ".gitignore")
        with open(gi, "w", encoding="utf-8") as f:
            f.write("*.tmp\n")

        path = os.path.join(self.td, "build.tmp")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")

        info = describe_overwrite_risk(path)
        self.assertTrue(info["ignored"])
        self.assertEqual(info["ignore_type"], "ext")
        self.assertEqual(info["risk_level"], "medium")
        self.assertEqual(info["system_action"], "block")

    def test_ignored_path_is_high_and_block(self):
        gi = os.path.join(self.td, ".gitignore")
        with open(gi, "w", encoding="utf-8") as f:
            f.write("exports/\n")

        os.makedirs(os.path.join(self.td, "exports"), exist_ok=True)
        path = os.path.join(self.td, "exports", "artifact.zip")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")

        info = describe_overwrite_risk(path)
        self.assertTrue(info["ignored"])
        self.assertEqual(info["ignore_type"], "path")
        self.assertEqual(info["risk_level"], "high")
        self.assertEqual(info["system_action"], "block")

    def test_untracked_is_critical_and_block(self):
        path = os.path.join(self.td, "new_work.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")

        info = describe_overwrite_risk(path)
        self.assertTrue(info["untracked"])
        self.assertEqual(info["risk_level"], "critical")
        self.assertEqual(info["system_action"], "block")

    def test_multi_match_escalates_to_path_high(self):
        gi = os.path.join(self.td, ".gitignore")
        with open(gi, "w", encoding="utf-8") as f:
            f.write("exports/\n")
            f.write("*.zip\n")

        os.makedirs(os.path.join(self.td, "exports"), exist_ok=True)
        path = os.path.join(self.td, "exports", "bundle.zip")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")

        info = describe_overwrite_risk(path)
        self.assertEqual(info["ignore_type"], "path")
        self.assertEqual(info["risk_level"], "high")
        self.assertEqual(info["system_action"], "block")


class TestGranularForceMatrix(unittest.TestCase):
    def test_required_force_flag_mapping(self):
        self.assertEqual(required_force_flag("medium"), "--force-medium")
        self.assertEqual(required_force_flag("high"), "--force-high")
        self.assertEqual(required_force_flag("critical"), "--force-critical")
        self.assertEqual(required_force_flag("low"), "")

    def test_none_default_and_restrictive_flag(self):
        permitted, reason = is_write_permitted("none")
        self.assertTrue(permitted)
        self.assertEqual(reason, "none_default")

        permitted2, reason2 = is_write_permitted("none", no_force_none=True)
        self.assertFalse(permitted2)
        self.assertEqual(reason2, "no_force_none")

    def test_low_default_and_restrictive_flag(self):
        permitted, reason = is_write_permitted("low")
        self.assertTrue(permitted)
        self.assertEqual(reason, "low_default")

        permitted2, reason2 = is_write_permitted("low", no_force_low=True)
        self.assertFalse(permitted2)
        self.assertEqual(reason2, "no_force_low")

    def test_medium_high_critical_require_explicit_force(self):
        self.assertEqual(is_write_permitted("medium"), (False, "require_force_medium"))
        self.assertEqual(is_write_permitted("high"), (False, "require_force_high"))
        self.assertEqual(is_write_permitted("critical"), (False, "require_force_critical"))

        self.assertEqual(is_write_permitted("medium", force_medium=True), (True, "force_medium"))
        self.assertEqual(is_write_permitted("high", force_high=True), (True, "force_high"))
        self.assertEqual(is_write_permitted("critical", force_critical=True), (True, "force_critical"))

    def test_global_force_bypasses_matrix(self):
        self.assertEqual(is_write_permitted("critical", force=True), (True, "global_force"))


if __name__ == "__main__":
    unittest.main()
