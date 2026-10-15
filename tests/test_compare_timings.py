"""scripts/compare_timings.py: grouping, medians and report flags, on synthetic records."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.script_test_loader import load_script_module

compare_timings = load_script_module("compare_timings.py", "compare_timings_under_test")


def _write_run(folder: Path, name: str, label: str, cases: dict, **meta) -> None:
    """cases: test name -> (status, seconds[, message])."""
    parts = []
    for test, (status, seconds, *message) in cases.items():
        inner = ""
        if status == "skipped":
            inner = f'<skipped message="{message[0] if message else "skip"}"/>'
        elif status == "failed":
            inner = f'<failure message="{message[0] if message else "boom"}"/>'
        parts.append(f'<testcase classname="tests.test_mod.TestX" name="{test}" time="{seconds}">{inner}</testcase>')
    total = sum(c[1] for c in cases.values())
    (folder / f"{name}.xml").write_text(
        f'<testsuites><testsuite name="pytest" time="{total}">{"".join(parts)}</testsuite></testsuites>',
        encoding="utf-8")
    data = {"label": label, "commit": "abc", "hardware": {"id": "hw1"}, "extras": [], **meta}
    (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


class TestCompareTimings(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_repeats_become_one_run_with_medians_and_range(self):
        for n, secs in enumerate((1.0, 3.0, 2.0), 1):
            _write_run(self.dir, f"native-r{n}", "native", {"a": ("passed", secs)}, end_to_end_seconds=secs + 1)
        runs = compare_timings.collect([str(self.dir)])
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual((run.repeats, run.suite_seconds, run.suite_range), (3, 2.0, (1.0, 3.0)))
        self.assertEqual(run.meta["end_to_end_seconds"], 3.0)
        self.assertEqual(run.tests["tests.test_mod.TestX.a"][1], 2.0)

    def test_pass_and_fail_across_repeats_is_flaky_and_reported(self):
        _write_run(self.dir, "x-r1", "x", {"a": ("passed", 1.0)})
        _write_run(self.dir, "x-r2", "x", {"a": ("failed", 1.0, "AssertionError")})
        _write_run(self.dir, "y", "y", {"a": ("passed", 1.0)})
        runs = compare_timings.collect([str(self.dir)])
        x = next(r for r in runs if r.label == "x")
        self.assertEqual(x.tests["tests.test_mod.TestX.a"][0], "flaky")
        report = compare_timings.report(runs, "y", 5)
        self.assertIn("3. Failures in only some environments", report)
        self.assertIn("AssertionError", report)

    def test_same_label_from_different_commits_is_not_merged(self):
        _write_run(self.dir, "native-old", "native", {"a": ("failed", 1.0, "boom")}, commit="aaaaaaa1")
        _write_run(self.dir, "native-new", "native", {"a": ("passed", 2.0)}, commit="bbbbbbb2")
        runs = compare_timings.collect([str(self.dir)])
        self.assertEqual(sorted(r.label for r in runs), ["native [aaaaaaa hw hw1]", "native [bbbbbbb hw hw1]"])
        self.assertTrue(all(r.repeats == 1 for r in runs))
        self.assertNotIn("flaky", {r.tests["tests.test_mod.TestX.a"][0] for r in runs})
        report = compare_timings.report(runs, None, 5)
        self.assertIn("runs come from different commits", report)

    def test_failure_is_not_outvoted_by_skips_and_keeps_its_message(self):
        _write_run(self.dir, "x-r1", "x", {"a": ("skipped", 0.0, "deferred")})
        _write_run(self.dir, "x-r2", "x", {"a": ("skipped", 0.0, "deferred")})
        _write_run(self.dir, "x-r3", "x", {"a": ("failed", 1.0, "AssertionError: real problem")})
        run = compare_timings.collect([str(self.dir)])[0]
        status, _, message = run.tests["tests.test_mod.TestX.a"]
        self.assertEqual((status, message), ("failed", "AssertionError: real problem"))
        self.assertEqual(run.count("failed"), 1)

    def test_skipped_repeats_do_not_shrink_a_passing_median(self):
        _write_run(self.dir, "x-r1", "x", {"a": ("skipped", 0.0, "deferred")})
        _write_run(self.dir, "x-r2", "x", {"a": ("skipped", 0.0, "deferred")})
        _write_run(self.dir, "x-r3", "x", {"a": ("passed", 9.0)})
        status, seconds, _ = compare_timings.collect([str(self.dir)])[0].tests["tests.test_mod.TestX.a"]
        self.assertEqual((status, seconds), ("passed", 9.0))

    def test_busy_start_is_flagged(self):
        _write_run(self.dir, "quiet", "quiet", {"a": ("passed", 1.0)}, cpu_busy_start=5.0)
        _write_run(self.dir, "busy", "busy", {"a": ("passed", 1.0)}, cpu_busy_start=80.0)
        report = compare_timings.report(compare_timings.collect([str(self.dir)]), "quiet", 5)
        self.assertIn("Busy machine: busy (80%)", report)
        self.assertNotIn("quiet (5%)", report)

    def test_scope_and_hardware_differences_are_named(self):
        _write_run(self.dir, "base", "base", {"a": ("passed", 1.0), "b": ("skipped", 0.0, "deferred")})
        _write_run(self.dir, "other", "other", {"a": ("passed", 1.0), "b": ("passed", 1.0)},
                   hardware={"id": "hw2"}, extras=["native checks"])
        report = compare_timings.report(compare_timings.collect([str(self.dir)]), "base", 5)
        self.assertIn("other: different (or unknown) hardware", report)
        self.assertIn("runs 1 test(s) the baseline doesn't", report)
        self.assertIn("skip: deferred", report)

    def test_cpu_busy_percent_is_a_percentage_or_none(self):
        value = compare_timings.cpu_busy_percent(sample_seconds=0.1)
        self.assertTrue(value is None or 0.0 <= value <= 100.0, value)


if __name__ == "__main__":
    unittest.main()
