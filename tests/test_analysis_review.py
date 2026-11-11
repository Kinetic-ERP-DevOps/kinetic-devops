"""Review-hardening tests for the AI diff review (analysis.py): verdict parsing, untrusted input,
the commit cap and the request deadline. No model is called."""
import unittest

from kinetic_devops import analysis


class TestFallbackVerdict(unittest.TestCase):
    """Without a leading VERDICT line, GO is never inferred; NO-GO (incl. negated GO) still is."""

    def test_not_go_in_final_section_is_not_go(self):
        answer = ("1) Change Classification\nPermission check change.\n\n"
                  "6) GO/NO-GO for further promotion\nDo NOT GO: the check now fails open.")
        self.assertNotEqual(analysis.parse_verdict(answer), "GO")
        self.assertEqual(analysis.parse_verdict(answer), "NO-GO")

    def test_bare_go_without_verdict_line_is_unknown(self):
        for answer in ("6) GO/NO-GO for further promotion\nGO", "looks fine, GO",
                       "6) GO/NO-GO\nGO, but not until the test is fixed"):
            with self.subTest(answer=answer):
                self.assertEqual(analysis.parse_verdict(answer), "UNKNOWN")

    def test_explicit_verdict_line_still_decides(self):
        self.assertEqual(analysis.parse_verdict("VERDICT: GO\n6) GO/NO-GO\nGO: safe"), "GO")
        self.assertEqual(analysis.parse_verdict("VERDICT: NO-GO\n..."), "NO-GO")


if __name__ == "__main__":
    unittest.main()
