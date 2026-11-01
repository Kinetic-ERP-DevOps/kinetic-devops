"""The version follows the rule in README (Versioning). Until 1.0.0: PEP 440 0.BREAKING.FEATURES.FIXES
plus stage and sequence number (a8). A Breaking section bumps BREAKING, Added/Changed bump FEATURES,
anything else FIXES; the sequence number goes up by exactly 1 per release and restarts at 1 when the
stage moves forward (a -> b -> rc).

Checked from CHANGELOG.md and pyproject.toml only, so it needs no git tags (CI checkouts are shallow).
"""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\] - \d{4}-\d{2}-\d{2}$", re.M)
RULE = re.compile(r"^0\.(\d+)\.(\d+)\.(\d+)(a|b|rc)(\d+)$")   # 0.BREAKING.FEATURES.FIXES + stage N
LEGACY = re.compile(r"^0\.(\d+)\.(\d+)(a|b|rc)(\d+)$")        # 0.1.0a1-a7, before the rule
STAGES = ("a", "b", "rc")


def parse(version):
    """(breaking, features, fixes), stage, n -- for rule versions and the legacy 0.x.y aN ones."""
    if m := RULE.match(version):
        return tuple(int(x) for x in m.group(1, 2, 3)), m.group(4), int(m.group(5))
    if m := LEGACY.match(version):
        return (int(m.group(1)), int(m.group(2)), 0), m.group(3), int(m.group(4))
    raise AssertionError(f"{version!r} is neither 0.BREAKING.FEATURES.FIXES{{a|b|rc}}N nor a legacy 0.x.y aN")


def bump(numbers, section):
    breaking, features, fixes = numbers
    heads = set(re.findall(r"^### (\w+)", section, re.M))
    if "Breaking" in heads:
        return (breaking + 1, 0, 0), "Breaking section: BREAKING + 1"
    if heads & {"Added", "Changed"}:
        return (breaking, features + 1, 0), "Added/Changed section: FEATURES + 1"
    if heads:
        return (breaking, features, fixes + 1), "fixes only: FIXES + 1"
    return numbers, "no changes listed: numbers unchanged"


def expected_next(previous, section, stage):
    """The version that must follow `previous` for a release with this CHANGELOG section and stage."""
    numbers, prev_stage, n = parse(previous)
    if STAGES.index(stage) < STAGES.index(prev_stage):
        raise AssertionError(f"stage goes backwards after {previous}: {prev_stage} -> {stage}")
    new, why = bump(numbers, section)
    if new == numbers and stage == prev_stage:
        raise AssertionError(f"release after {previous} lists no changes and keeps the stage")
    seq = n + 1 if stage == prev_stage else 1
    return "0.{}.{}.{}{}{}".format(*new, stage, seq), why


def released_sections():
    """[(version, section text)] for released versions, newest first."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heads = list(HEADING.finditer(text))
    return [(h["version"], text[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)])
            for i, h in enumerate(heads)]


class TestVersioning(unittest.TestCase):
    def setUp(self):
        self.releases = released_sections()
        self.assertTrue(self.releases, "CHANGELOG.md has no released versions")

    def test_pyproject_version_is_the_newest_release(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["version"], self.releases[0][0],
                         "pyproject.toml version must match the newest released CHANGELOG heading")

    def test_each_release_follows_the_rule(self):
        self.assertRegex(self.releases[0][0], RULE,
                         "newest release must be 0.BREAKING.FEATURES.FIXES plus a|b|rc and a number (README, Versioning)")
        checked = []
        for (version, section), (previous, _) in zip(self.releases, self.releases[1:]):
            if not RULE.match(version):
                break   # older releases predate the rule
            stage = RULE.match(version).group(4)
            want, why = expected_next(previous, section, stage)
            self.assertEqual(version, want, f"{version} after {previous} ({why})")
            checked.append(f"{previous} -> {version} ({why})")
        self.assertTrue(checked, "no release under the versioning rule found")
        print("versioning: checked " + "; ".join(checked))

    def test_rule_examples(self):
        self.assertEqual(expected_next("0.1.0a7", "### Breaking\n", "a")[0], "0.2.0.0a8")
        self.assertEqual(expected_next("0.2.0.0a8", "### Added\n### Fixed\n", "a")[0], "0.2.1.0a9")
        self.assertEqual(expected_next("0.2.1.0a9", "### Fixed\n", "a")[0], "0.2.1.1a10")
        self.assertEqual(expected_next("0.2.1.3a12", "### Breaking\n", "a")[0], "0.3.0.0a13")
        self.assertEqual(expected_next("0.5.2.0a20", "", "b")[0], "0.5.2.0b1")
        for bad in (("0.5.2.0b3", "### Fixed\n", "a"), ("0.2.0.0a8", "", "a")):
            with self.assertRaises(AssertionError):
                expected_next(*bad)
        with self.assertRaises(AssertionError):
            parse("0.2.0")   # every release before 1.0.0 carries a stage


if __name__ == "__main__":
    unittest.main()
