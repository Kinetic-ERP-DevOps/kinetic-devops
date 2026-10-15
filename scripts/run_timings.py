#!/usr/bin/env python
"""Run the test suite in each local environment and compare them (informative only).

Environments, each skipped with a note when unavailable:
  windows-native   this machine, `uv run python -m pytest` (native checks
                   deferred unless --native)
  windows-sandbox  the kept Windows Sandbox via scripts/sandbox_test.py
                   (native checks + Windows smoke checks included)
  wsl              a WSL distro with uv installed (native checks included;
                   Linux launchers are scripts, not unsigned .exe files)

All run the same commit (or the uncommitted tracked changes with
--working-tree). Records -- JUnit XML plus a metadata file per environment --
go to temp/timings/<commit>/, together with any CI records you download there
(see scripts/README.md), and the report from scripts/compare_timings.py is
printed at the end.

    uv run python scripts/run_timings.py                     # all available
    uv run python scripts/run_timings.py --envs native,wsl --working-tree
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Sibling scripts (scripts/ is on sys.path when this file is run as a script).
import compare_timings
import sandbox_test

ENVS = ("native", "sandbox", "wsl")
LABELS = {"native": "windows-native", "sandbox": "windows-sandbox", "wsl": "wsl"}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _wsl_path(path: Path) -> str:
    resolved = path.resolve()
    return f"/mnt/{resolved.drive[0].lower()}{resolved.as_posix()[2:]}"


def run_native(repo_root: Path, out_dir: Path, sha: str, native: bool) -> str:
    xml = out_dir / f"{LABELS['native']}.xml"
    started = time.monotonic()
    env = {**os.environ, "KINETIC_NATIVE_CHECKS": "1"} if native else None
    subprocess.run(["uv", "run", "python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    f"--junitxml={xml}"], cwd=repo_root, env=env)
    extras = ["native checks"] if native else ["native checks deferred"]
    compare_timings.write_meta(LABELS["native"], xml.with_suffix(".json"), time.monotonic() - started,
                               extras, sha)
    return "ok" if xml.exists() else "no JUnit XML produced"


def run_sandbox(out_dir: Path, sha: str) -> str:
    xml = out_dir / f"{LABELS['sandbox']}.xml"
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
                extras=["native checks", "windows smoke checks", "bundle transfer + uv sync"],
                hardware=compare_timings.hardware(), virtualized=True)  # VM on this machine
    meta.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return "ok"


def run_wsl(repo_root: Path, out_dir: Path, sha: str, distro: str) -> str:
    if not shutil.which("wsl"):
        return "WSL not installed"
    started_ok = subprocess.run(["wsl", "-d", distro, "-e", "true"], capture_output=True)
    if started_ok.returncode != 0:
        # wsl.exe writes UTF-16; keep the first line of whatever it said.
        detail = started_ok.stdout.replace(b"\x00", b"").decode(errors="replace").strip().splitlines()
        return f"WSL distro '{distro}' did not start: {detail[0] if detail else 'no output'}"
    probe = subprocess.run(["wsl", "-d", distro, "-e", "sh", "-c", "test -x ~/.local/bin/uv || command -v uv"],
                           capture_output=True)
    if probe.returncode != 0:
        return f"uv not found in WSL distro '{distro}' (install: curl -LsSf https://astral.sh/uv/install.sh | sh)"
    bundle = out_dir / "wsl-repo.bundle"
    sandbox_test._bundle(repo_root, sha, bundle)
    xml = out_dir / f"{LABELS['wsl']}.xml"
    meta = xml.with_suffix(".json")
    # The checkout lives on the Linux filesystem (fast); results go back via /mnt.
    script = f"""
set -e
export PATH="$HOME/.local/bin:$PATH" KINETIC_NATIVE_CHECKS=1
D="$HOME/kd-timings"
rm -rf "$D" && git init -q "$D" && cd "$D"
git fetch -q '{_wsl_path(bundle)}' '+refs/kinetic-sandbox/*:refs/kinetic-sandbox/*'
git checkout -q --detach {sha}
start=$(date +%s)
uv sync --locked -q
uv run python -m pytest -q -p no:cacheprovider --junitxml='{_wsl_path(xml)}' || true
uv run python scripts/compare_timings.py meta --label wsl --out '{_wsl_path(meta)}' \\
    --end-to-end $(( $(date +%s) - start )) --extra 'native checks' --commit {sha}
cd && rm -rf "$D"
"""
    subprocess.run(["wsl", "-d", distro, "-e", "bash", "-lc", script])
    bundle.unlink(missing_ok=True)
    if meta.exists():  # WSL is a VM on this machine: record this machine's hardware
        data = json.loads(meta.read_text(encoding="utf-8"))
        data.update(hardware=compare_timings.hardware(), virtualized=True)
        meta.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return "ok" if xml.exists() else "no JUnit XML produced"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the suite in each local environment (Windows native, Windows Sandbox, WSL) "
                    "for one commit and print an informative comparison. Never fails on timings."
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
    parser.add_argument("--wsl-distro", default="Ubuntu", help="WSL distro to use (default: Ubuntu)")
    parser.add_argument("--out", type=Path, help="Records folder (default: temp/timings/<commit>)")
    args = parser.parse_args(argv)

    envs = [e.strip() for e in args.envs.split(",") if e.strip()]
    unknown = sorted(set(envs) - set(ENVS))
    if unknown:
        parser.error(f"unknown environment(s): {', '.join(unknown)}")

    repo_root = Path(_git("rev-parse", "--show-toplevel", cwd=Path.cwd()))
    if args.working_tree:
        sha = sandbox_test.snapshot_working_tree(repo_root)
    else:
        sha = _git("rev-parse", "--verify", f"{args.commit}^{{commit}}", cwd=repo_root)
    out_dir = args.out or repo_root / "temp" / "timings" / sha[:12]
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"run_timings: commit {sha[:12]}; records in {out_dir}")

    status = {}
    if "native" in envs:
        # The native run always tests the working tree, so it matches the other
        # environments only for --working-tree, or a clean tree at the commit.
        head = _git("rev-parse", "HEAD", cwd=repo_root)
        dirty = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=repo_root).returncode != 0
        if args.working_tree or (sha == head and not dirty):
            print("run_timings: windows-native ...")
            status["native"] = run_native(repo_root, out_dir, sha, args.native)
        else:
            status["native"] = ("skipped: this machine runs the working tree, which isn't the commit "
                                "tested elsewhere (use --working-tree, or a clean tree at that commit)")
    if "sandbox" in envs:
        print("run_timings: windows-sandbox ...")
        status["sandbox"] = run_sandbox(out_dir, sha)
    if "wsl" in envs:
        print(f"run_timings: wsl ({args.wsl_distro}) ...")
        status["wsl"] = run_wsl(repo_root, out_dir, sha, args.wsl_distro)

    print("\nEnvironments: " + "; ".join(f"{LABELS[e]}: {s}" for e, s in status.items()) + "\n")
    print(compare_timings.report(compare_timings.collect([str(out_dir)]), LABELS["native"], 12))
    return 0


if __name__ == "__main__":
    sys.exit(main())
