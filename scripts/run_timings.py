#!/usr/bin/env python
"""Run the test suite in each local environment and compare them (informative only).

Environments, each skipped with a note when unavailable:
  windows-native   this machine, `uv run python -m pytest` (native checks
                   deferred unless --native)
  windows-sandbox  the kept Windows Sandbox via scripts/sandbox_test.py
                   (native checks + Windows smoke checks included)
  wsl              a WSL distro with uv installed (default: the newest
                   Ubuntu-NN.NN that has uv; native checks included -- Linux
                   launchers are scripts, not unsigned .exe files)
  alpine           a throwaway python:3.14-alpine container (musl) run with
                   Podman in a WSL distro that has it

All run the same commit (or the working tree with --working-tree). Records --
JUnit XML plus a metadata file per run -- go to temp/timings/<commit>/,
together with any CI records you download there (see scripts/README.md), and
the report from scripts/compare_timings.py is printed at the end. With
--repeat N each environment runs N times and the report compares medians.
Each record notes how busy this machine's CPU was when the run started, and
the report flags busy runs, whose timings are unreliable.

    uv run python scripts/run_timings.py                     # all available
    uv run python scripts/run_timings.py --envs native,wsl --working-tree --repeat 3
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Sibling scripts (scripts/ is on sys.path when this file is run as a script).
import compare_timings
import sandbox_test

ENVS = ("native", "sandbox", "wsl", "alpine")
LABELS = {"native": "windows-native", "sandbox": "windows-sandbox", "wsl": "wsl", "alpine": "alpine-podman"}
ALPINE_IMAGE = "docker.io/library/python:3.14-alpine"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _wsl_path(path: Path) -> str:
    resolved = path.resolve()
    return f"/mnt/{resolved.drive[0].lower()}{resolved.as_posix()[2:]}"


def _wsl_text(raw: bytes) -> str:
    # wsl.exe itself writes UTF-16; commands run inside the distro write UTF-8.
    return raw.replace(b"\x00", b"").decode(errors="replace").strip()


def _wsl(distro: str, command: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["wsl", "-d", distro, "-e", "sh", "-c", command], capture_output=True, **kwargs)


def wsl_distros() -> list[str]:
    if not shutil.which("wsl"):
        return []
    listing = subprocess.run(["wsl", "-l", "-q"], capture_output=True)
    return [line.strip() for line in _wsl_text(listing.stdout).splitlines() if line.strip()]


def _ubuntu_first(distros: list[str]) -> list[str]:
    """Versioned Ubuntu distros newest first, then the rest in listed order."""
    def key(name: str):
        match = re.fullmatch(r"Ubuntu-(\d+)\.(\d+)", name)
        return (0, -int(match.group(1)), -int(match.group(2))) if match else (1, 0, 0)
    return sorted(distros, key=key)


def find_wsl_distro(requested: str | None, probe: str, what: str) -> tuple[str | None, str]:
    """(distro, note): the requested distro, or the first (newest Ubuntu first)
    where `probe` succeeds."""
    candidates = [requested] if requested else _ubuntu_first(wsl_distros())
    if not candidates:
        return None, "WSL not installed"
    problems = []
    for distro in candidates:
        result = _wsl(distro, probe)
        if result.returncode == 0:
            return distro, ""
        detail = _wsl_text(result.stdout + result.stderr).splitlines()
        problems.append(f"{distro}: {detail[0] if detail else what + ' not found'}")
    return None, f"no WSL distro with {what} ({'; '.join(problems)})"


def _stamp_vm(meta: Path, busy: float | None, end_to_end: float | None = None) -> None:
    """Runs in WSL, a container or the sandbox use this machine's hardware.
    end_to_end, measured on this machine around the whole run (setup included,
    like the native and Sandbox runs), replaces any duration measured inside."""
    if meta.exists():
        data = json.loads(meta.read_text(encoding="utf-8"))
        data.update(hardware=compare_timings.hardware(), virtualized=True, cpu_busy_start=busy)
        if end_to_end is not None:
            data["end_to_end_seconds"] = round(end_to_end, 1)
        meta.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run_native(repo_root: Path, xml: Path, sha: str, native: bool) -> str:
    busy = compare_timings.cpu_busy_percent()
    started = time.monotonic()
    env = {**os.environ, "KINETIC_NATIVE_CHECKS": "1"} if native else None
    subprocess.run(["uv", "run", "python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    f"--junitxml={xml}"], cwd=repo_root, env=env)
    extras = ["native checks"] if native else ["native checks deferred"]
    compare_timings.write_meta(LABELS["native"], xml.with_suffix(".json"), time.monotonic() - started,
                               extras, sha, cpu_busy_start=busy)
    return "ok" if xml.exists() else "no JUnit XML produced"


def run_sandbox(xml: Path, sha: str) -> str:
    busy = compare_timings.cpu_busy_percent()
    started = time.monotonic()
    code = sandbox_test.run(sha, "full", True, 30, False, False, False, False, junit_out=xml)
    if code == sandbox_test.UNAVAILABLE:
        return "Windows Sandbox unavailable or occupied"
    if not xml.exists():
        return "no JUnit XML produced"
    # The sandbox writes its own metadata (its Python/OS); add the host-side view.
    meta = xml.with_suffix(".json")
    data = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {
        "label": LABELS["sandbox"], "python": "?", "os": "Windows Sandbox", "cpus": None}
    data.update(commit=sha, end_to_end_seconds=round(time.monotonic() - started, 1),
                extras=["native checks", "windows smoke checks", "bundle transfer + uv sync"])
    meta.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _stamp_vm(meta, busy)
    if code != 0:
        # The timing record is kept, but the Windows smoke checks run outside
        # pytest, so the JUnit report can't show their failure -- say it here.
        return (f"FAILED (sandbox exit {code}: test suite or Windows smoke checks failed; "
                "timing record kept, see the sandbox job logs)")
    return "ok"


def run_wsl(repo_root: Path, xml: Path, sha: str, distro: str) -> str:
    busy = compare_timings.cpu_busy_percent()
    started = time.monotonic()  # end-to-end on the host: bundle, checkout, sync, tests
    bundle = xml.with_suffix(".bundle")
    sandbox_test._bundle(repo_root, sha, bundle)
    meta = xml.with_suffix(".json")
    q = _sh_quote
    # The checkout lives on the Linux filesystem (fast), in a fresh temporary
    # directory of its own; results go back via /mnt.
    script = f"""
set -e
export PATH="$HOME/.local/bin:$PATH" KINETIC_NATIVE_CHECKS=1
D=$(mktemp -d "${{TMPDIR:-/tmp}}/kd-timings.XXXXXX")
trap 'rm -rf "$D"' EXIT
git init -q "$D" && cd "$D"
git fetch -q {q(_wsl_path(bundle))} '+refs/kinetic-sandbox/*:refs/kinetic-sandbox/*'
git checkout -q --detach {q(sha)}
uv sync --locked -q
uv run python -m pytest -q -p no:cacheprovider --junitxml={q(_wsl_path(xml))} || true
uv run python scripts/compare_timings.py meta --label {q(LABELS['wsl'])} --out {q(_wsl_path(meta))} \
    --extra 'native checks' --commit {q(sha)}
"""
    subprocess.run(["wsl", "-d", distro, "-e", "bash", "-lc", script])
    bundle.unlink(missing_ok=True)
    _stamp_vm(meta, busy, time.monotonic() - started)
    return "ok" if xml.exists() else "no JUnit XML produced"


def run_alpine(repo_root: Path, xml: Path, sha: str, distro: str) -> str:
    busy = compare_timings.cpu_busy_percent()
    started = time.monotonic()  # end-to-end on the host: incl. container start, image pull, installs
    bundle = xml.with_suffix(".bundle")
    sandbox_test._bundle(repo_root, sha, bundle)
    meta = xml.with_suffix(".json")
    q = _sh_quote
    # A throwaway container; the records folder is mounted at /out.
    inner = f"""
set -e
apk add --no-cache git >/dev/null
pip install -q --root-user-action=ignore uv
git init -q /w && cd /w
git fetch -q {q('/out/' + bundle.name)} '+refs/kinetic-sandbox/*:refs/kinetic-sandbox/*'
git checkout -q --detach {q(sha)}
export UV_PYTHON_DOWNLOADS=never KINETIC_NATIVE_CHECKS=1 UV_LINK_MODE=copy
uv sync --locked -q
uv run python -m pytest -q -p no:cacheprovider --junitxml={q('/out/' + xml.name)} || true
uv run python scripts/compare_timings.py meta --label {q(LABELS['alpine'])} --out {q('/out/' + meta.name)} \
    --extra 'native checks' --commit {q(sha)}
"""
    mount = _wsl_path(xml.parent) + ":/out"
    outer = f"podman run --rm -v {q(mount)} {ALPINE_IMAGE} sh -c {q(inner)}"
    subprocess.run(["wsl", "-d", distro, "-e", "sh", "-c", outer])
    bundle.unlink(missing_ok=True)
    _stamp_vm(meta, busy, time.monotonic() - started)
    return "ok" if xml.exists() else "no JUnit XML produced"


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the suite in each local environment (Windows native, Windows Sandbox, WSL, "
                    "Alpine container) for one commit and print an informative comparison. "
                    "Never fails on timings."
    )
    parser.add_argument("--envs", default=",".join(ENVS),
                        help=f"Comma-separated subset of {', '.join(ENVS)} (default: all available)")
    parser.add_argument("--commit", default="HEAD", help="Commit to test (default: HEAD)")
    parser.add_argument("--working-tree", action="store_true",
                        help="Test the working tree as it is (including untracked, non-ignored files) "
                             "instead of a commit")
    parser.add_argument("--native", action="store_true",
                        help="Also run the deferred native checks on this machine (intentional; "
                             "endpoint security may block the launchers)")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="Run each environment N times; the report compares medians (default: 1)")
    parser.add_argument("--wsl-distro", help="WSL distro for 'wsl' (default: newest Ubuntu-NN.NN with uv)")
    parser.add_argument("--podman-distro", help="WSL distro running Podman for 'alpine' (default: first with podman)")
    parser.add_argument("--out", type=Path, help="Records folder (default: temp/timings/<commit>)")
    args = parser.parse_args(argv)

    envs = [e.strip() for e in args.envs.split(",") if e.strip()]
    unknown = sorted(set(envs) - set(ENVS))
    if unknown:
        parser.error(f"unknown environment(s): {', '.join(unknown)}")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    repo_root = Path(_git("rev-parse", "--show-toplevel", cwd=Path.cwd()))
    if args.working_tree:
        sha = sandbox_test.snapshot_working_tree(repo_root)
    else:
        sha = _git("rev-parse", "--verify", f"{args.commit}^{{commit}}", cwd=repo_root)
    # Absolute from the start: subprocesses (pytest, git bundle) run from the
    # repo root, so a caller-relative --out must not be reinterpreted there.
    out_dir = (args.out.resolve() if args.out else repo_root / "temp" / "timings" / sha[:12])
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"run_timings: commit {sha[:12]}; records in {out_dir}")

    def record(env: str, n: int) -> Path:
        suffix = f"-r{n}" if args.repeat > 1 else ""
        return out_dir / f"{LABELS[env]}{suffix}.xml"

    # Resolve each environment once; unavailable ones are skipped with a reason.
    runners, status = {}, {}
    if "native" in envs:
        # The native run always tests the working tree, so it matches the other
        # environments only for --working-tree, or a clean tree at the commit.
        head = _git("rev-parse", "HEAD", cwd=repo_root)
        # Untracked, non-ignored files count: a new tests/test_*.py would run
        # here but be missing from the commit tested elsewhere.
        dirty = bool(_git("status", "--porcelain", "--untracked-files=normal", cwd=repo_root))
        if args.working_tree or (sha == head and not dirty):
            runners["native"] = lambda xml: run_native(repo_root, xml, sha, args.native)
        else:
            status["native"] = ("skipped: this machine runs the working tree, which isn't the commit "
                                "tested elsewhere -- it has changes or untracked files (use "
                                "--working-tree, or a clean tree at that commit)")
    if "sandbox" in envs:
        runners["sandbox"] = lambda xml: run_sandbox(xml, sha)
    if "wsl" in envs:
        distro, note = find_wsl_distro(args.wsl_distro, "test -x ~/.local/bin/uv || command -v uv", "uv")
        if distro:
            print(f"run_timings: wsl uses {distro}")
            runners["wsl"] = lambda xml, d=distro: run_wsl(repo_root, xml, sha, d)
        else:
            status["wsl"] = f"skipped: {note} (install uv: curl -LsSf https://astral.sh/uv/install.sh | sh)"
    if "alpine" in envs:
        distro, note = find_wsl_distro(args.podman_distro, "command -v podman", "podman")
        if distro:
            print(f"run_timings: alpine uses Podman in {distro}")
            runners["alpine"] = lambda xml, d=distro: run_alpine(repo_root, xml, sha, d)
        else:
            status["alpine"] = f"skipped: {note}"

    # The report reads every record in the folder, so remove this tool's
    # earlier records for the environments being rerun (e.g. an old -r3 series
    # from a previous --repeat 3). Anything else -- e.g. downloaded CI
    # records -- is kept.
    for env in runners:
        label = LABELS[env]
        for old in list(out_dir.glob(f"{label}.*")) + list(out_dir.glob(f"{label}-r*.*")):
            if old.suffix in (".xml", ".json", ".bundle") and re.fullmatch(
                    rf"{re.escape(label)}(-r\d+)?", old.stem):
                old.unlink(missing_ok=True)

    for n in range(1, args.repeat + 1):
        for env, runner in runners.items():
            print(f"run_timings: {LABELS[env]} (run {n}/{args.repeat}) ...")
            xml = record(env, n)
            # Never let a report from an earlier invocation stand in for this
            # run: if setup fails before pytest writes, the run must show as failed.
            for stale in (xml, xml.with_suffix(".json"), xml.with_suffix(".bundle")):
                stale.unlink(missing_ok=True)
            result = runner(xml)
            if result != "ok" or env not in status:
                status[env] = result

    print("\nEnvironments: " + "; ".join(f"{LABELS[e]}: {s}" for e, s in status.items()) + "\n")
    print(compare_timings.report(compare_timings.collect([str(out_dir)]), LABELS["native"], 12))
    return 0


if __name__ == "__main__":
    sys.exit(main())
