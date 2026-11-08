"""Forgejo workflow steps run in a BusyBox shell: no GNU-only coreutils options."""

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".forgejo" / "workflows"


class TestForgejoWorkflowPortability(unittest.TestCase):
    def test_mktemp_uses_only_posix_options(self):
        """BusyBox mktemp has no long options (sync #66: `mktemp --suffix=.py` failed every run)."""
        files = sorted(WORKFLOWS.glob("*.yml"))
        self.assertTrue(files, f"no workflows under {WORKFLOWS}")
        bad = [f"{f.name}:{n}: {line.strip()}"
               for f in files
               for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
               if re.search(r"\bmktemp\b[^\n|;)]*\s--", line)]
        self.assertEqual(bad, [], "GNU-only mktemp options:\n" + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
