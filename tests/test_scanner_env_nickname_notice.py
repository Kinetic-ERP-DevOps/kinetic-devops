"""Environment nicknames are reported as notices, never as failures."""

import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kinetic_devops import find_sensitive_data
from kinetic_devops.auth import KineticConfigManager


class TestNicknamePattern(unittest.TestCase):
    def _patterns(self, servers):
        manager = KineticConfigManager.__new__(KineticConfigManager)
        with mock.patch.object(KineticConfigManager, "_get_server_dict", return_value=servers):
            return manager.get_sensitive_data_patterns()

    def test_builds_case_sensitive_notice_pattern_from_nicknames(self):
        patterns = self._patterns({"AlphaEnv": {}, "Beta": {}, "QA": {}})
        pattern = patterns["NOTICE_ENV_NICKNAMES"]
        self.assertTrue(pattern.search("deployed to AlphaEnv today"))
        self.assertTrue(pattern.search("Beta was refreshed"))
        self.assertIsNone(pattern.search("the beta channel"), "matching is case-sensitive")
        self.assertIsNone(pattern.search("QA team"), "names shorter than 3 characters are ignored")

    def test_no_servers_means_no_notice_pattern(self):
        self.assertEqual(self._patterns({}), {})


class TestNoticesDoNotFail(unittest.TestCase):
    def _scan(self, text, patterns):
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        out = io.StringIO()
        argv = ["find_sensitive_data", "--file", path, "--fail-on-findings", "--no-generic-base64"]
        try:
            with mock.patch.object(find_sensitive_data, "get_sensitive_data_from_keyring", return_value=patterns), \
                    mock.patch.object(sys, "argv", argv), redirect_stdout(out):
                code = find_sensitive_data.main()
        finally:
            os.remove(path)
        return code, out.getvalue()

    def test_notice_only_scan_passes_and_reports_notice(self):
        code, output = self._scan("ran it on AlphaEnv\n", {"NOTICE_ENV_NICKNAMES": re.compile(r"\b(AlphaEnv)\b")})
        self.assertEqual(code, 0)
        self.assertIn("Notices", output)
        self.assertIn("NOTICE_ENV_NICKNAMES", output)
        self.assertIn("No sensitive data found.", output)

    def test_real_findings_still_fail_alongside_notices(self):
        patterns = {"NOTICE_ENV_NICKNAMES": re.compile(r"\b(AlphaEnv)\b"), "FAKE_SECRET": re.compile(r"s3cr3t-[0-9]+")}
        code, output = self._scan("AlphaEnv s3cr3t-12345\n", patterns)
        self.assertEqual(code, 1)
        self.assertIn("Sensitive data found", output)
        self.assertIn("FAKE_SECRET", output)


if __name__ == "__main__":
    unittest.main()
