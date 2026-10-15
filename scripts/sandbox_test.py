#!/usr/bin/env python
"""Test a commit on a clean Windows install using Windows Sandbox.

The checks that run generated native code -- uv's console-script .exe
launchers and the .ps1 scripts -- are deferred on a development machine,
because enterprise endpoint security (e.g. Carbon Black) may block newly built
or edited unsigned files. This runs them, and as much of the suite as you ask
for, in Windows Sandbox: a clean Windows with no endpoint-security agent and
nothing from this machine except the checkout and a read-only view of the local
Git install. Takes several minutes; the sandbox window closes itself when done.

Usage:
    uv run python scripts/sandbox_test.py                 # full suite + smoke checks
    uv run python scripts/sandbox_test.py --native-only   # only the deferred native checks
    uv run python scripts/sandbox_test.py --tests tests.test_cli tests.test_auth_rename
    uv run python scripts/sandbox_test.py --commit HEAD~1 --keep-open

Exit codes: 0 passed, 1 failed (or timed out), 2 Windows Sandbox unavailable or
already running (nothing was tested). Commits that pass a full run are recorded
in <git-dir>/kinetic-sandbox-passed; the opt-in pre-push hook skips those, so
you can test ahead of a push, and a retried push is instant.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

SANDBOX_EXE = Path(r"C:\Windows\System32\WindowsSandbox.exe")
UNAVAILABLE = 2
# The suite's tests that execute generated launchers (deferred locally).
NATIVE_TESTS = ["tests.test_cli"]


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _sandbox_running() -> bool:
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq WindowsSandboxRemoteSession.exe", "/NH"],
        capture_output=True, text=True,
    )
    return "WindowsSandboxRemoteSession" in result.stdout


def _git_install_root() -> Path:
    # <root>\mingw64\libexec\git-core -> <root> (standard and Scoop installs)
    root = Path(_git("--exec-path")).resolve().parents[2]
    if not (root / "cmd" / "git.exe").is_file():
        raise RuntimeError(f"Could not find cmd\\git.exe under {root}")
    return root


def _wsb_config(in_dir: Path, out_dir: Path, git_root: Path) -> str:
    def folder(host: Path, guest: str, read_only: bool) -> str:
        return (
            "    <MappedFolder>\n"
            f"      <HostFolder>{escape(str(host))}</HostFolder>\n"
            f"      <SandboxFolder>{guest}</SandboxFolder>\n"
            f"      <ReadOnly>{'true' if read_only else 'false'}</ReadOnly>\n"
            "    </MappedFolder>\n"
        )

    command = r"powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\kd-in\repo\scripts\sandbox_run.ps1"
    return (
        "<Configuration>\n"
        "  <Networking>Enable</Networking>\n"
        "  <vGPU>Disable</vGPU>\n"
        "  <ClipboardRedirection>Disable</ClipboardRedirection>\n"
        "  <PrinterRedirection>Disable</PrinterRedirection>\n"
        "  <AudioInput>Disable</AudioInput>\n"
        "  <VideoInput>Disable</VideoInput>\n"
        "  <MemoryInMB>8192</MemoryInMB>\n"
        "  <MappedFolders>\n"
        + folder(in_dir, r"C:\kd-in", True)
        + folder(out_dir, r"C:\kd-out", False)
        + folder(git_root, r"C:\Git", True)
        + "  </MappedFolders>\n"
        "  <LogonCommand>\n"
        f"    <Command>{command}</Command>\n"
        "  </LogonCommand>\n"
        "</Configuration>\n"
    )


def run(commit: str, suite, smoke: bool, timeout_min: int, keep_open: bool, keep_files: bool) -> int:
    if sys.platform != "win32" or not SANDBOX_EXE.is_file():
        print("sandbox_test: Windows Sandbox is not available on this machine; skipped.")
        return UNAVAILABLE
    if _sandbox_running():
        print("sandbox_test: a Windows Sandbox is already running (only one is allowed); "
              "close it and retry. Skipped.")
        return UNAVAILABLE

    repo_root = Path(_git("rev-parse", "--show-toplevel"))
    sha = _git("rev-parse", "--verify", f"{commit}^{{commit}}", cwd=repo_root)
    work = Path(tempfile.mkdtemp(prefix="kd-sandbox-"))
    in_dir, out_dir = work / "in", work / "out"
    out_dir.mkdir(parents=True)
    in_dir.mkdir()

    _git("clone", "--quiet", "--no-checkout", str(repo_root), str(in_dir / "repo"))
    _git("checkout", "--quiet", sha, cwd=in_dir / "repo")
    options = {"suite": suite, "smoke": smoke, "keep_open": keep_open}
    (in_dir / "options.json").write_text(json.dumps(options), encoding="utf-8")

    wsb = work / "kd-sandbox.wsb"
    wsb.write_text(_wsb_config(in_dir, out_dir, _git_install_root()), encoding="utf-8")

    print(f"sandbox_test: testing {sha[:12]} in Windows Sandbox ({json.dumps(options)}).")
    print(f"sandbox_test: work folder {work}; the sandbox window opens, this takes several minutes...")
    subprocess.Popen([str(SANDBOX_EXE), str(wsb)])

    result_file = out_dir / "RESULT"
    deadline = time.monotonic() + timeout_min * 60
    while not result_file.exists() and time.monotonic() < deadline:
        time.sleep(10)

    summary = out_dir / "summary.txt"
    if summary.exists():
        print(summary.read_text(encoding="utf-8", errors="replace"))
    if not result_file.exists():
        print(f"sandbox_test: no result after {timeout_min} minutes; logs in {out_dir}")
        return 1

    passed = result_file.read_text(encoding="utf-8", errors="replace").strip().endswith("PASS")
    if passed and suite == "full" and smoke:
        record = Path(_git("rev-parse", "--path-format=absolute", "--git-path",
                           "kinetic-sandbox-passed", cwd=repo_root))
        with record.open("a", encoding="utf-8") as fh:
            fh.write(f"{sha}\n")
    if passed and not keep_files and not keep_open:
        time.sleep(15)  # let the sandbox shut down and release the mapped folders
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"sandbox_test: step logs are in {out_dir}")
    print(f"sandbox_test: {'PASSED' if passed else 'FAILED'}")
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run tests for a commit on a clean Windows in Windows Sandbox, including the native "
            "checks (.exe launchers, .ps1 scripts) that local runs defer. Default: the full suite "
            "plus the Windows smoke checks. Exit 0 pass, 1 fail, 2 sandbox unavailable or running."
        )
    )
    parser.add_argument("--commit", default="HEAD", help="Commit to test (default: HEAD)")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--native-only", action="store_true",
                       help=f"Only the deferred native checks: {' '.join(NATIVE_TESTS)} plus the smoke checks")
    scope.add_argument("--tests", nargs="+", metavar="NAME",
                       help="Run only these unittest names (e.g. tests.test_cli or "
                            "tests.test_cli.TestCli.test_x) instead of the full suite")
    scope.add_argument("--smoke-only", action="store_true",
                       help="Only scripts/windows_smoke.ps1, no Python tests")
    parser.add_argument("--no-smoke", action="store_true",
                        help="Skip scripts/windows_smoke.ps1")
    parser.add_argument("--timeout", type=int, default=30, metavar="MIN",
                        help="Give up after this many minutes (default: 30)")
    parser.add_argument("--keep-open", action="store_true",
                        help="Leave the sandbox running after the run, for inspection")
    parser.add_argument("--keep-files", action="store_true",
                        help="Keep the temporary checkout and logs even when the run passes")
    args = parser.parse_args(argv)

    if args.native_only:
        suite = NATIVE_TESTS
    elif args.tests:
        suite = args.tests
    elif args.smoke_only:
        suite = "none"
    else:
        suite = "full"
    smoke = not args.no_smoke
    if suite == "none" and not smoke:
        parser.error("--smoke-only and --no-smoke together leave nothing to run")
    return run(args.commit, suite, smoke, args.timeout, args.keep_open, args.keep_files)


if __name__ == "__main__":
    sys.exit(main())
