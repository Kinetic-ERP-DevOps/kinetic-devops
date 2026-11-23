"""Case-sensitive allowed spellings for the sensitive data scanner.

A spelling listed under "sensitive_scan_allow" in the local site file is let through only
when the match is exactly that spelling, so allowing "zzq" and "Zzq" still flags "ZZQ".
"""

import json
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch

from kinetic_devops import find_sensitive_data as fsd

PATTERNS = {"EXAMPLE": re.compile(r"\b(ZZQ)\b", re.IGNORECASE)}


class TestLoadAllowedMatches(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.mkdtemp(prefix="scan_allow_test_")
        self.path = os.path.join(self.td, "site.json")

    def tearDown(self) -> None:
        shutil.rmtree(self.td, ignore_errors=True)

    def _write(self, data) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def test_reads_the_allow_key_and_keeps_case(self):
        self._write({"other": ["x"], "sensitive_scan_allow": ["zzq", "Zzq"]})
        self.assertEqual(fsd.load_allowed_matches(self.path), frozenset({"zzq", "Zzq"}))

    def test_missing_file_allows_nothing(self):
        self.assertEqual(fsd.load_allowed_matches(os.path.join(self.td, "absent.json")), frozenset())

    def test_file_without_the_key_allows_nothing(self):
        self._write({"other": ["x"]})
        self.assertEqual(fsd.load_allowed_matches(self.path), frozenset())

    def test_blank_entries_are_dropped(self):
        self._write({"sensitive_scan_allow": ["zzq", "", "  "]})
        self.assertEqual(fsd.load_allowed_matches(self.path), frozenset({"zzq"}))

    def test_bad_json_fails_loudly(self):
        self._write("{not json")
        with self.assertRaises(ValueError):
            fsd.load_allowed_matches(self.path)

    def test_non_list_value_fails_loudly(self):
        self._write({"sensitive_scan_allow": "zzq"})
        with self.assertRaises(ValueError):
            fsd.load_allowed_matches(self.path)

    def test_env_var_names_the_file(self):
        self._write({"sensitive_scan_allow": ["zzq"]})
        with patch.dict(os.environ, {fsd.SITE_FILE_ENV_VAR: self.path}):
            self.assertEqual(fsd.load_allowed_matches(), frozenset({"zzq"}))


class TestAllowedMatchesInScan(unittest.TestCase):
    def _matches(self, line, allowed):
        with patch.object(fsd, "ALLOWED_MATCHES", frozenset(allowed)):
            return [m for _, m in fsd._collect_line_matches(line, PATTERNS)]

    def test_allowed_spellings_pass_and_other_spellings_are_still_flagged(self):
        self.assertEqual(self._matches("zzq Zzq ZZQ zZq", {"zzq", "Zzq"}), ["ZZQ", "zZq"])

    def test_nothing_allowed_flags_every_spelling(self):
        self.assertEqual(self._matches("zzq ZZQ", set()), ["zzq", "ZZQ"])

    def test_allow_is_whole_match_only(self):
        # "zz" is not the match "zzq", so it does not let it through.
        self.assertEqual(self._matches("zzq", {"zz"}), ["zzq"])


if __name__ == "__main__":
    unittest.main()
