import unittest
import sys
import os

# Add project root to path to allow importing kinetic_devops
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from kinetic_devops.KineticCore import KineticCore

class TestRedaction(unittest.TestCase):
    def setUp(self):
        # Create a safe subclass to bypass __init__ side effects (keyring, salts)
        class SafeCore(KineticCore):
            def __init__(self):
                pass
        self.core = SafeCore()

    def test_keywords_redaction(self):
        """Test that specific keywords from your regex trigger redaction."""
        cases = {
            '{"Company": "Epicor"}': '{"Company": "[REDACTED]"}',
            '{"ApiKey": "12345"}': '{"ApiKey": "[REDACTED]"}',
            '{"Token": "abc-def"}': '{"Token": "[REDACTED]"}',
            '{"Email": "user@example.com"}': '{"Email": "[REDACTED]"}',
            '{"Plant": "MfgSys"}': '{"Plant": "[REDACTED]"}',
        }
        for input_str, expected in cases.items():
            self.assertEqual(self.core._heuristic_redact(input_str), expected, f"Failed on {input_str}")

    def test_suffixes_and_variations(self):
        """Test suffixes like ID, Num, Ref and case insensitivity."""
        cases = {
            '{"PartNum": "P-100"}': '{"PartNum": "[REDACTED]"}',   # Part + Num
            '{"CustID": "C-500"}': '{"CustID": "[REDACTED]"}',     # Cust + ID
            '{"Order-Ref": "123"}': '{"Order-Ref": "[REDACTED]"}', # Order + Ref
            '{"ship_val": "10"}': '{"ship_val": "[REDACTED]"}',    # Ship + Val
            '{"sysrowid": "uuid"}': '{"sysrowid": "[REDACTED]"}',  # Case insensitive
        }
        for input_str, expected in cases.items():
            self.assertEqual(self.core._heuristic_redact(input_str), expected)

    def test_partial_matches(self):
        """Test that the regex matches keys starting with keywords (e.g. Desc -> Description)."""
        input_str = '{"Description": "Sensitive Info"}'
        expected = '{"Description": "[REDACTED]"}'
        self.assertEqual(self.core._heuristic_redact(input_str), expected)

    def test_non_sensitive_data(self):
        """Test that non-sensitive keys are preserved."""
        input_str = '{"Status": "OK", "Count": 100, "IsActive": true}'
        self.assertEqual(self.core._heuristic_redact(input_str), input_str)

    def test_nested_json_structure(self):
        """Test redaction within nested JSON."""
        input_str = '{"Response": {"SysRowID": "uuid-123", "Data": "Keep"}}'
        expected = '{"Response": {"SysRowID": "[REDACTED]", "Data": "Keep"}}'
        self.assertEqual(self.core._heuristic_redact(input_str), expected)

    def test_whitespace_variations(self):
        """Test redaction with various whitespace scenarios."""
        cases = {
            '  "Company"  :   "Epicor"  ': '  "Company"  :   "[REDACTED]"  ',
            '\t"ApiKey"\t:\t"12345"\t': '\t"ApiKey"\t:\t"[REDACTED]"\t',
            '\n"Token"\n:\n"abc"\n': '\n"Token"\n:\n"[REDACTED]"\n',
        }
        for input_str, expected in cases.items():
            self.assertEqual(self.core._heuristic_redact(input_str), expected)

    def test_escaped_nested_json(self):
        """Test redaction of keys containing escaped JSON strings (typical of Epicor headers)."""
        # Simulates a context header with nested escaped JSON containing sensitive data
        input_str = r'"CallContextBpmData": "{\"Context\":{\"BpmData\":[{\"Password\":\"Secret\"}]}}"'
        expected = r'"CallContextBpmData": "{\"Context\":{\"BpmData\":[{\"Password\":\"[REDACTED]\"}]}}"'
        self.assertEqual(self.core._heuristic_redact(input_str), expected)

if __name__ == '__main__':
    unittest.main()