#!/usr/bin/env python
"""Compare test-suite runs across environments (informative only; never fails).

Each run is a JUnit XML file from `python -m pytest --junitxml=<label>.xml`,
optionally with a `<label>.json` metadata file next to it (written by
`compare_timings.py meta`): environment label, commit, Python, OS, CPUs, the
end-to-end time of the whole path (setup, transfer, sync, extra checks) and
what extra work it included.

    uv run python scripts/compare_timings.py report temp/timings/<commit>/ [--baseline windows-native]
    uv run python scripts/compare_timings.py meta --label linux-ci --out timings/linux-ci.json

The report shows:
  1. Totals per environment -- suite time and end-to-end time, with the scope
     (tests run / skipped / failed, extras), and where scopes differ from the
     baseline, what differs. Totals are shown even when scopes differ: they
     answer "what does this path cost", just not "which machine is faster".
  2. Tests not run everywhere, with each environment's skip reason.
  3. Failures that happen in only some environments.
  4. Per-module and per-test times over the tests that ran in every
     environment (the like-for-like comparison).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Run:
    label: str
    meta: dict
    # test id -> (status, seconds, message); status: passed|skipped|failed|error
    tests: dict = field(default_factory=dict)
    suite_seconds: float = 0.0
    repeats: int = 1
    suite_range: tuple | None = None  # (min, max) suite seconds when repeated

    def count(self, status: str) -> int:
        return sum(1 for s, _, _ in self.tests.values() if s == status)


def load_run(xml_path: Path) -> Run:
    root = ET.parse(xml_path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    meta_path = xml_path.with_suffix(".json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    run = Run(label=meta.get("label") or xml_path.stem, meta=meta)
    for suite in suites:
        run.suite_seconds += float(suite.get("time") or 0)
        for case in suite.iter("testcase"):
            test_id = f"{case.get('classname')}.{case.get('name')}"
            status, message = "passed", ""
            for kind in ("skipped", "failure", "error"):
                element = case.find(kind)
                if element is not None:
                    status = {"failure": "failed"}.get(kind, kind)
                    message = (element.get("message") or "").strip().splitlines()[0:1]
                    message = message[0] if message else ""
                    break
            run.tests[test_id] = (status, float(case.get("time") or 0), message)
    return run


def collect(paths: list[str]) -> list[Run]:
    """Load runs; repeated runs of one environment (same label) become one
    Run with median times."""
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        files.extend(sorted(path.glob("*.xml")) if path.is_dir() else [path])
    return combine([load_run(f) for f in files])


def _context(run: Run) -> tuple:
    """What must match for two records to count as repeats of one run."""
    meta = run.meta
    return (run.label, meta.get("commit", ""), (meta.get("hardware") or {}).get("id", ""),
            meta.get("python", ""), tuple(sorted(meta.get("extras", []))))


def combine(runs: list[Run]) -> list[Run]:
    """Merge repeats -- records with the same label AND the same commit,
    hardware, Python and extra checks -- into one Run with median times.
    Records that share a label but differ in context stay separate, with the
    differing part added to the label so they can be told apart."""
    groups: dict[tuple, list[Run]] = {}
    for run in runs:
        groups.setdefault(_context(run), []).append(run)
    label_counts: dict[str, int] = {}
    for key in groups:
        label_counts[key[0]] = label_counts.get(key[0], 0) + 1
    used: set[str] = set()
    for key, group in groups.items():
        if label_counts[key[0]] > 1:  # same label, different context
            _, commit, hw, python, _ = key
            suffix = " ".join(filter(None, [commit[:7], f"hw {hw}" if hw else "", f"py {python}" if python else ""]))
            name, n = f"{key[0]} [{suffix}]", 2
            while name in used:  # e.g. differing only in extra checks
                name, n = f"{key[0]} [{suffix} #{n}]", n + 1
            used.add(name)
            for run in group:
                run.label = name
    combined = []
    for key, group in groups.items():
        label = group[0].label
        if len(group) == 1:
            combined.append(group[0])
            continue
        meta = dict(group[0].meta)
        ends = [r.meta.get("end_to_end_seconds") for r in group if r.meta.get("end_to_end_seconds") is not None]
        meta["end_to_end_seconds"] = statistics.median(ends) if ends else None
        busy = [r.meta.get("cpu_busy_start") for r in group if r.meta.get("cpu_busy_start") is not None]
        meta["cpu_busy_start"] = max(busy) if busy else None
        merged = Run(label=label, meta=meta, repeats=len(group),
                     suite_seconds=statistics.median(r.suite_seconds for r in group),
                     suite_range=(min(r.suite_seconds for r in group), max(r.suite_seconds for r in group)))
        for test in set().union(*(r.tests for r in group)):
            results = [r.tests[test] for r in group if test in r.tests]
            statuses = {status for status, _, _ in results}
            bad = statuses & {"failed", "error"}
            if bad and "passed" in statuses:
                status = "flaky"  # passed in some repeats, failed in others
            elif bad:
                # Never let skips outvote a failure: no repeat passed.
                status = "error" if "error" in bad else "failed"
            elif "passed" in statuses:
                status = "passed"  # ran and passed at least once, never failed
            else:
                status = results[0][0]
            # A failure's diagnostic beats a skip reason.
            message = next((m for st, _, m in results if st in ("failed", "error") and m), "") or next(
                (m for st, _, m in results if st != "passed" and m), "")
            # Time from executions only: a ~0 s skip says nothing about how long
            # the test takes. Skipped durations count only if every repeat skipped.
            executed = [t for st, t, _ in results if st != "skipped"]
            seconds = statistics.median(executed or [t for _, t, _ in results])
            merged.tests[test] = (status, seconds, message)
        combined.append(merged)
    return combined


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]
    line = lambda cells: "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths])] + [line(r) for r in rows])


def _secs(value) -> str:
    return "-" if value in (None, "") else f"{float(value):.1f}s"


def _ratio(value: float, base: float) -> str:
    return "" if not base else f" (x{value / base:.2f})"


def report(runs: list[Run], baseline: str | None, top: int) -> str:
    if not runs:
        return "No runs found."
    base = next((r for r in runs if r.label == baseline), runs[0])
    out: list[str] = []

    commits = {r.meta.get("commit", "?")[:12] for r in runs}
    if len(commits) > 1:
        out.append(f"NOTE: runs come from different commits ({', '.join(sorted(commits))}); "
                   "differences may be code changes, not environments.\n")

    # 1. Totals
    rows = []
    for r in runs:
        rows.append([
            r.label + (" *" if r is base else ""),
            r.meta.get("python", "?"), r.meta.get("os", "?"), str(r.meta.get("cpus", "?")),
            (r.meta.get("hardware") or {}).get("id", "?") + (" (VM)" if r.meta.get("virtualized") else ""),
            str(r.repeats),
            "-" if r.meta.get("cpu_busy_start") is None else f"{r.meta['cpu_busy_start']:.0f}%",
            str(r.count("passed") + r.count("failed") + r.count("error") + r.count("flaky")),
            str(r.count("skipped")), str(r.count("failed") + r.count("error") + r.count("flaky")),
            _secs(r.suite_seconds) + _ratio(r.suite_seconds, base.suite_seconds)
            + (f" [{r.suite_range[0]:.1f}-{r.suite_range[1]:.1f}]" if r.suite_range else ""),
            _secs(r.meta.get("end_to_end_seconds")),
            ", ".join(r.meta.get("extras", [])),
        ])
    out.append("1. Totals (* = baseline; with repeats: medians, suite [min-max]; Busy = this machine's CPU "
               "use when the run started)\n" + _table(
        ["Environment", "Python", "OS", "CPUs", "Hardware", "Runs", "Busy", "Ran", "Skipped", "Failed", "Suite",
         "End-to-end", "Extras"], rows))
    busy_runs = [r for r in runs if (r.meta.get("cpu_busy_start") or 0) >= BUSY_THRESHOLD]
    if busy_runs:
        out.append("\nBusy machine: " + ", ".join(f"{r.label} ({r.meta['cpu_busy_start']:.0f}%)" for r in busy_runs)
                   + f" started with the CPU at least {BUSY_THRESHOLD}% busy -- timings unreliable; "
                   "rerun when idle or use --repeat.")

    base_ran = {t for t, (s, _, _) in base.tests.items() if s != "skipped"}
    notes = []
    for r in runs:
        if r is base:
            continue
        ran = {t for t, (s, _, _) in r.tests.items() if s != "skipped"}
        only_here, only_base = sorted(ran - base_ran), sorted(base_ran - ran)
        extras_differ = set(r.meta.get("extras", [])) != set(base.meta.get("extras", []))
        hw, base_hw = (r.meta.get("hardware") or {}).get("id"), (base.meta.get("hardware") or {}).get("id")
        hardware_differs = not hw or not base_hw or hw != base_hw
        if only_here or only_base or extras_differ or hardware_differs:
            parts = []
            if hardware_differs:
                parts.append("different (or unknown) hardware -- totals compare machines, not just environments")
            if only_here:
                parts.append(f"runs {len(only_here)} test(s) the baseline doesn't")
            if only_base:
                parts.append(f"doesn't run {len(only_base)} test(s) the baseline runs")
            if extras_differ:
                parts.append("different extra checks")
            notes.append(f"  {r.label}: " + "; ".join(parts))
    out.append("\nScope vs baseline -- totals are apples to oranges where listed:\n"
               + ("\n".join(notes) if notes else "  all environments ran the same tests and extras"))

    # 2. Not run everywhere
    all_tests = sorted(set().union(*(r.tests for r in runs)))
    rows = []
    for test in all_tests:
        states = [r.tests.get(test) for r in runs]
        if all(s is not None and s[0] != "skipped" for s in states):
            continue
        cells = []
        for s in states:
            if s is None:
                cells.append("absent")
            elif s[0] == "skipped":
                cells.append("skip: " + s[2][:60])
            else:
                cells.append(s[0])
        rows.append([test.split("tests.", 1)[-1]] + cells)
    out.append("\n2. Tests not run in every environment\n"
               + (_table(["Test"] + [r.label for r in runs], rows) if rows else "  none"))

    # 3. Environment-only failures
    rows = []
    for test in all_tests:
        failed = [r for r in runs if r.tests.get(test, ("",))[0] in ("failed", "error", "flaky")]
        if failed and len(failed) < len(runs):
            for r in failed:
                rows.append([test.split("tests.", 1)[-1], r.label, r.tests[test][2][:90]])
    out.append("\n3. Failures in only some environments\n"
               + (_table(["Test", "Environment", "Message"], rows) if rows else "  none"))

    # 4. Like-for-like: tests that ran (passed) everywhere
    common = [t for t in all_tests if all(r.tests.get(t, ("",))[0] == "passed" for r in runs)]
    out.append(f"\n4. Like-for-like over the {len(common)} tests that passed in every environment")
    per_module: dict[str, list[float]] = defaultdict(lambda: [0.0] * len(runs))
    for test in common:
        module = test.split(".")[1] if test.startswith("tests.") else test.rsplit(".", 2)[0]
        for i, r in enumerate(runs):
            per_module[module][i] += r.tests[test][1]
    base_index = runs.index(base)
    totals = [sum(r.tests[t][1] for t in common) for r in runs]
    rows = [["(all common tests)"] + [_secs(v) + _ratio(v, totals[base_index]) for v in totals]]
    for module, values in sorted(per_module.items(), key=lambda kv: -max(kv[1]))[:top]:
        rows.append([module] + [_secs(v) + _ratio(v, values[base_index]) for v in values])
    out.append(_table(["Module"] + [r.label for r in runs], rows))

    if len(runs) > 1:
        spread = sorted(common, key=lambda t: -(max(r.tests[t][1] for r in runs) - min(r.tests[t][1] for r in runs)))
        rows = [[t.split("tests.", 1)[-1][:80]] + [_secs(r.tests[t][1]) for r in runs] for t in spread[:top]]
        out.append(f"\nLargest per-test differences (top {top})\n" + _table(["Test"] + [r.label for r in runs], rows))
    return "\n".join(out)


def _ram_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong),
                            ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                            ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                            ("avail_ext", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return round(status.total / 2**30, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (AttributeError, OSError, ValueError):
        return None


def hardware() -> dict:
    """This machine's hardware, with a short id instead of its hostname (records
    may be shared, e.g. as CI artifacts). Equal ids = same machine."""
    cpu = platform.processor() or platform.machine()
    ram = _ram_gb()
    fingerprint = f"{cpu}|{ram}|{os.cpu_count()}|{platform.node()}"
    return {"id": hashlib.sha256(fingerprint.encode()).hexdigest()[:8], "cpu": cpu, "ram_gb": ram,
            "cpus": os.cpu_count()}


BUSY_THRESHOLD = 50  # % CPU in use when a run starts; above this the report flags the run


def cpu_busy_percent(sample_seconds: float = 1.0) -> float | None:
    """Share of CPU time in use across all cores over a short sample, or None
    where it can't be measured. Recorded per run so the report can flag runs
    that started on a busy machine."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            def times():
                idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
                ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
                value = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime
                return value(idle), value(kernel) + value(user)  # kernel time includes idle
        else:
            def times():
                fields = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
                return fields[3] + (fields[4] if len(fields) > 4 else 0), sum(fields)
        idle1, total1 = times()
        time.sleep(sample_seconds)
        idle2, total2 = times()
        total = total2 - total1
        return round(100.0 * (1 - (idle2 - idle1) / total), 1) if total > 0 else None
    except (OSError, ValueError, AttributeError, IndexError):
        return None


def write_meta(label: str, out: Path, end_to_end: float | None, extras: list[str], commit: str | None,
               cpu_busy_start: float | None = None) -> None:
    if not commit:
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        except OSError:
            commit = ""
    meta = {
        "label": label,
        "commit": commit,
        "python": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}",
        "cpus": os.cpu_count(),
        "hardware": hardware(),
        "virtualized": False,
        "end_to_end_seconds": end_to_end,
        "extras": extras,
        "cpu_busy_start": cpu_busy_start,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare test-suite runs across environments (informative only).")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report", help="Print the comparison for JUnit XML files or folders of them")
    rep.add_argument("paths", nargs="+", help="JUnit XML files, or folders containing <label>.xml (+ <label>.json)")
    rep.add_argument("--baseline", help="Environment label to compare against (default: the first run)")
    rep.add_argument("--top", type=int, default=12, help="Rows in the per-module/per-test tables (default 12)")
    meta = sub.add_parser("meta", help="Write the metadata file for a run made in this environment")
    meta.add_argument("--label", required=True, help="Environment label, e.g. windows-native, linux-ci-py3.12")
    meta.add_argument("--out", required=True, type=Path, help="Path of the .json file (next to <label>.xml)")
    meta.add_argument("--end-to-end", type=float, help="Seconds for the whole path, if measured")
    meta.add_argument("--extra", action="append", default=[], help="Extra checks included (repeatable)")
    meta.add_argument("--commit", help="Commit tested (default: git HEAD)")
    args = parser.parse_args(argv)

    if args.command == "meta":
        write_meta(args.label, args.out, args.end_to_end, args.extra, args.commit)
        return 0
    print(report(collect(args.paths), args.baseline, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
