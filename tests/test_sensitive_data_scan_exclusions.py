"""Regression tests for sensitive data scanner exclusion behavior."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from kinetic_devops.find_sensitive_data import (
    DEFAULT_EXCLUDE_DIRS,
    GENERIC_PATTERNS,
    _should_report_generic_base64,
    get_files_to_scan,
    main,
    scan_git_history,
)


class _FakePopen:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.returncode = 0

    def wait(self):
        return 0


class TestSensitiveDataScanExclusions(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.mkdtemp(prefix="scan_excl_test_")

    def tearDown(self) -> None:
        shutil.rmtree(self.td, ignore_errors=True)

    def test_get_files_to_scan_normalizes_dot_slash_exclude_paths(self):
        os.makedirs(os.path.join(self.td, "exports"), exist_ok=True)
        os.makedirs(os.path.join(self.td, "src"), exist_ok=True)

        excluded_file = os.path.join(self.td, "exports", "artifact.json")
        kept_file = os.path.join(self.td, "src", "ok.py")
        with open(excluded_file, "w", encoding="utf-8") as f:
            f.write("SECRET")
        with open(kept_file, "w", encoding="utf-8") as f:
            f.write("print('ok')")

        files = get_files_to_scan(
            self.td,
            use_gitignore=False,
            exclude_dirs={".\\exports"},
        )

        rel_files = {os.path.relpath(p, self.td).replace("\\", "/") for p in files}
        self.assertIn("src/ok.py", rel_files)
        self.assertNotIn("exports/artifact.json", rel_files)

    def test_get_files_to_scan_honors_explicit_ignored_subpath(self):
        subprocess.run(["git", "init"], cwd=self.td, check=True, capture_output=True)
        with open(os.path.join(self.td, ".gitignore"), "w", encoding="utf-8") as f:
            f.write("exports/\n")

        os.makedirs(os.path.join(self.td, "exports"), exist_ok=True)
        explicit_file = os.path.join(self.td, "exports", "artifact.json")
        with open(explicit_file, "w", encoding="utf-8") as f:
            f.write("explicit-target")

        files = get_files_to_scan(
            os.path.join(self.td, "exports"),
            use_gitignore=True,
            exclude_dirs={"exports"},
        )

        rel_files = {os.path.relpath(p, self.td).replace("\\", "/") for p in files}
        self.assertIn("exports/artifact.json", rel_files)

    def test_scan_git_history_respects_excluded_paths(self):
        patterns = {"PRIVATE_KEY_BLOCK": re.compile(r"PRIVATE KEY")}  # kd-sensitive-scan-ignore-line: pattern fixture, not a secret
        fake_log = [
            "commit deadbeef\n",
            "diff --git a/exports/secret.txt b/exports/secret.txt\n",
            "+PRIVATE KEY SHOULD BE EXCLUDED\n",  # kd-sensitive-scan-ignore-line: fixture data, not a secret
            "diff --git a/src/app.py b/src/app.py\n",
            "+PRIVATE KEY SHOULD BE FOUND\n",  # kd-sensitive-scan-ignore-line: fixture data, not a secret
        ]

        with patch("kinetic_devops.find_sensitive_data.subprocess.Popen", return_value=_FakePopen(fake_log)):
            findings = scan_git_history(self.td, patterns, exclude_dirs={"exports", ".\\exports"})

        self.assertEqual(len(findings), 1)
        location, _, pattern_name, match = findings[0]
        self.assertIn("src/app.py", location)
        self.assertEqual(pattern_name, "PRIVATE_KEY_BLOCK")
        self.assertIn("FOUND", match)

    def test_scan_git_history_honors_explicit_include_path(self):
        patterns = {"PRIVATE_KEY_BLOCK": re.compile(r"PRIVATE KEY")}  # kd-sensitive-scan-ignore-line: pattern fixture, not a secret
        fake_log = [
            "commit deadbeef\n",
            "diff --git a/kinetic_devops/find_sensitive_data.py b/kinetic_devops/find_sensitive_data.py\n",
            "+PRIVATE KEY OUTSIDE SCOPE\n",  # kd-sensitive-scan-ignore-line: fixture data, not a secret
            "diff --git a/exports/inside.txt b/exports/inside.txt\n",
            "+PRIVATE KEY INSIDE SCOPE\n",  # kd-sensitive-scan-ignore-line: fixture data, not a secret
        ]

        with patch("kinetic_devops.find_sensitive_data.subprocess.Popen", return_value=_FakePopen(fake_log)):
            findings = scan_git_history(self.td, patterns, exclude_dirs=set(), include_path="./exports")

        self.assertEqual(len(findings), 1)
        location, _, pattern_name, match = findings[0]
        self.assertIn("exports/inside.txt", location)
        self.assertEqual(pattern_name, "PRIVATE_KEY_BLOCK")
        self.assertIn("INSIDE", match)

    def test_repo_scan_is_tracked_files_minus_explicit_excludes(self):
        """Scanned unless untracked or explicitly excluded; folder names (reports, temp...) never hide content."""
        subprocess.run(["git", "init"], cwd=self.td, check=True, capture_output=True)
        for folder in ("reports", "temp", "src", "skipme"):
            os.makedirs(os.path.join(self.td, folder), exist_ok=True)
            for name in ("tracked.txt", "untracked.txt"):
                with open(os.path.join(self.td, folder, name), "w", encoding="utf-8") as f:
                    f.write("x")
            subprocess.run(["git", "add", f"{folder}/tracked.txt"], cwd=self.td, check=True, capture_output=True)

        cwd = os.getcwd()
        os.chdir(self.td)
        try:
            files = get_files_to_scan(".", use_gitignore=True, exclude_dirs=set(DEFAULT_EXCLUDE_DIRS),
                                      tracked_exclude_dirs={"skipme"})
        finally:
            os.chdir(cwd)

        rel_files = {os.path.normpath(p).replace("\\", "/") for p in files}
        self.assertEqual(rel_files, {"reports/tracked.txt", "temp/tracked.txt", "src/tracked.txt"})

    def test_git_scans_get_only_explicit_excludes(self):
        captured = {}

        def fake_diff(*args, **kwargs):
            captured.update(kwargs)
            return []

        with patch("kinetic_devops.find_sensitive_data.scan_git_diff", side_effect=fake_diff),              patch("kinetic_devops.find_sensitive_data.find_sensitive_data", return_value=[]),              patch("kinetic_devops.find_sensitive_data._resolve_scan_files", return_value=[]),              patch.object(sys, "argv", ["find_sensitive_data", "--path", self.td, "--staged", "--no-keyring"]):
            main()
        self.assertEqual(captured["exclude_dirs"], set())

    def test_internal_address_patterns(self):
        hits = lambda text: sorted(n for n, rx in GENERIC_PATTERNS.items() if n.startswith("INTERNAL_") and rx.search(text))
        self.assertEqual(hits("host 192" ".168.10.5"), ["INTERNAL_PRIVATE_IPV4"])
        self.assertEqual(hits("172" ".20.1.1"), ["INTERNAL_PRIVATE_IPV4"])
        self.assertEqual(hits("100" ".101.5.6"), ["INTERNAL_TAILNET_IPV4"])
        self.assertEqual(hits("git.example" ".ts.net"), ["INTERNAL_TAILNET_HOST"])
        self.assertEqual(hits("fd7a:115c:" "a1e0:ab::1"), ["INTERNAL_TAILNET_IPV6"])
        # Version strings are split like the hits above: the sync promotion guard's coarser 10.x.x.x check
        # would otherwise refuse this file.
        for clean in ("192.0.2.10", "8.8.8.8", "Windows 10" ".0.19041.1", "pkg 10" ".2.0.300", "172.15.1.1"):
            self.assertEqual(hits(clean), [], clean)

    def test_cli_fail_on_findings_returns_nonzero(self):
        secret_file = os.path.join(self.td, "secret.txt")
        with open(secret_file, "w", encoding="utf-8") as f:
            f.write("PRIVATE KEY SHOULD FAIL\n")  # kd-sensitive-scan-ignore-line: fixture data, not a secret

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kinetic_devops.find_sensitive_data",
                "--file",
                secret_file,
                "--no-keyring",
                "--no-generic-base64",
                "--fail-on-findings",
            ],
            cwd=self.td,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            },
            timeout=60,
        )

        stdout_text = (result.stdout or b"").decode("utf-8", errors="replace")
        stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 1, msg=stdout_text + stderr_text)
        self.assertIn("Sensitive data found", stdout_text)

    def test_generic_base64_ignores_camel_case_identifier_by_default(self):
        self.assertFalse(_should_report_generic_base64("GetSolutionItemsAsDynamicDataSet"))

    def test_generic_base64_ignores_snake_case_identifier_by_default(self):
        self.assertFalse(_should_report_generic_base64("token_payload_blob_string_value"))

    def test_generic_base64_can_include_alpha_only_when_requested(self):
        self.assertTrue(
            _should_report_generic_base64(
                "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY",  # kd-sensitive-scan-ignore-line: fixture data, not a secret
                include_alpha_only=True,
            )
        )

    def test_generic_base64_can_include_snake_case_when_requested(self):
        self.assertTrue(
            _should_report_generic_base64(
                "YWJjX2RlZl9naGlfamtsX21ub19wcXJfc3R1",  # kd-sensitive-scan-ignore-line: fixture data, not a secret
                include_snake_case=True,
            )
        )

    def test_generic_base64_ignores_dash_separated_identifier_by_default(self):
        self.assertFalse(_should_report_generic_base64("manual-import-pause-complete"))

    def test_generic_base64_ignores_path_like_identifier_by_default(self):
        self.assertFalse(_should_report_generic_base64("reports/CustomReports/ReportName"))

    def test_generic_base64_ignores_repeated_punctuation(self):
        self.assertFalse(_should_report_generic_base64("--------------------------------"))


if __name__ == "__main__":
    unittest.main()
