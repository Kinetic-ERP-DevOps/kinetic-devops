#!/usr/bin/env python
"""Test a commit on a clean Windows install using Windows Sandbox.

The checks that run generated native code -- uv's console-script .exe
launchers and the .ps1 scripts -- are deferred on a development machine,
because enterprise endpoint security may block newly built
or edited unsigned files. This runs them, and as much of the suite as you ask
for, in Windows Sandbox: a Windows with no endpoint-security agent and nothing
from this machine except the job folder and a read-only view of the local Git
install.

With the Windows Sandbox CLI (`wsb`, Windows 11 24H2+), one sandbox is kept
running and reused: every run first checks what is running, reuses the sandbox
it started earlier, or starts one if none is running. Each job gets a fresh
checkout and .venv; uv and its cache stay warm, so later runs are much faster
than the first. Without `wsb` it falls back to a one-shot sandbox per run.

Usage:
    uv run python scripts/sandbox_test.py                 # full suite + smoke checks
    uv run python scripts/sandbox_test.py --native-only   # only the deferred native checks
    uv run python scripts/sandbox_test.py --tests tests.test_cli tests.test_auth_rename
    uv run python scripts/sandbox_test.py --fresh         # restart the sandbox first (clean machine)
    uv run python scripts/sandbox_test.py --stop          # stop the kept sandbox and exit

A sandbox that is already running but unknown to this tool (a stale id, an
earlier session, or one opened by hand) is adopted: the job folder and Git are
shared into it with `wsb share`, so it never blocks a run.

Exit codes: 0 passed, 1 failed (or timed out), 2 Windows Sandbox unavailable
(nothing was tested). Commits
that pass a full run are recorded in <git-dir>/kinetic-sandbox-passed; the
opt-in pre-push hook skips those, so you can test ahead of a push.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

SANDBOX_EXE = Path(r"C:\Windows\System32\WindowsSandbox.exe")
UNAVAILABLE = 2
# The suite's native checks (deferred locally): tests that execute generated
# launchers or load newly installed native libraries.
NATIVE_TESTS = ["tests.test_cli", "tests.test_auth_rename.TestEncryptedVault"]
# Host folder shared read-write with the kept sandbox as C:\kd-jobs.
BASE = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "kinetic-devops" / "sandbox"
STATE = BASE / "sandbox.json"
JOBS = BASE / "jobs"
GUEST_BASE = r"C:\kd-jobs"
WORKING_TREE = ":working-tree:"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_install_root() -> Path:
    # <root>\mingw64\libexec\git-core -> <root> (standard and Scoop installs)
    root = Path(_git("--exec-path")).resolve().parents[2]
    if not (root / "cmd" / "git.exe").is_file():
        raise RuntimeError(f"Could not find cmd\\git.exe under {root}")
    return root


def _wsb_config(folders: list[tuple[Path, str, bool]], logon_command: str = "") -> str:
    mapped = "".join(
        "    <MappedFolder>\n"
        f"      <HostFolder>{escape(str(host))}</HostFolder>\n"
        f"      <SandboxFolder>{guest}</SandboxFolder>\n"
        f"      <ReadOnly>{'true' if read_only else 'false'}</ReadOnly>\n"
        "    </MappedFolder>\n"
        for host, guest, read_only in folders
    )
    logon = (
        f"  <LogonCommand>\n    <Command>{escape(logon_command)}</Command>\n  </LogonCommand>\n"
        if logon_command else ""
    )
    return (
        "<Configuration>\n"
        "  <Networking>Enable</Networking>\n"
        "  <vGPU>Disable</vGPU>\n"
        "  <ClipboardRedirection>Disable</ClipboardRedirection>\n"
        "  <PrinterRedirection>Disable</PrinterRedirection>\n"
        "  <AudioInput>Disable</AudioInput>\n"
        "  <VideoInput>Disable</VideoInput>\n"
        "  <MemoryInMB>8192</MemoryInMB>\n"
        f"  <MappedFolders>\n{mapped}  </MappedFolders>\n"
        f"{logon}"
        "</Configuration>\n"
    )


def _checkout(repo_root: Path, sha: str, dest: Path) -> None:
    _git("clone", "--quiet", "--no-checkout", str(repo_root), str(dest))
    _git("checkout", "--quiet", sha, cwd=dest)


def snapshot_working_tree(repo_root: Path) -> str:
    """Commit object for the working tree as it is -- tracked changes plus
    untracked, non-ignored files -- built in a temporary index, so the real
    index, stash list and refs are untouched (`git stash create` would miss
    untracked files)."""
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(td) / "index")}

        def git(*args: str) -> str:
            return subprocess.run(["git", *args], cwd=repo_root, env=env, check=True,
                                  capture_output=True, text=True).stdout.strip()

        git("read-tree", "HEAD")
        git("add", "-A")
        tree = git("write-tree")
        if tree == git("rev-parse", "HEAD^{tree}"):
            return git("rev-parse", "HEAD")
        return git("commit-tree", tree, "-p", "HEAD", "-m", "sandbox_test: working tree snapshot")


def _bundle(repo_root: Path, sha: str, dest: Path) -> None:
    """Write `sha` (and its history) to a git bundle file. Bundles carry refs, so
    point a temporary ref under refs/kinetic-sandbox/ at the commit (it may be an
    unreferenced `git stash create` commit) and remove it again. `git clone`
    from a bundle only takes branches and tags, so the sandbox fetches
    refs/kinetic-sandbox/* explicitly."""
    ref = f"refs/kinetic-sandbox/{os.getpid()}"
    _git("update-ref", ref, sha, cwd=repo_root)
    try:
        _git("bundle", "create", str(dest), ref, cwd=repo_root)
    finally:
        _git("update-ref", "-d", ref, cwd=repo_root)


def _print_summary(out_dir: Path) -> None:
    summary = out_dir / "summary.txt"
    if summary.exists():
        print(summary.read_text(encoding="utf-8", errors="replace"))


def _record_pass(repo_root: Path, sha: str) -> None:
    record = Path(_git("rev-parse", "--path-format=absolute", "--git-path",
                       "kinetic-sandbox-passed", cwd=repo_root))
    with record.open("a", encoding="utf-8") as fh:
        fh.write(f"{sha}\n")


# --- kept sandbox via the wsb CLI -------------------------------------------

def _wsb_path() -> str | None:
    return shutil.which("wsb")


def _wsb(*args: str, timeout: float | None = 120) -> dict:
    result = subprocess.run([_wsb_path(), *args, "--raw"], capture_output=True, text=True, timeout=timeout)
    text = result.stdout.strip()
    try:
        return json.loads(text) if text else {}
    except ValueError:
        raise RuntimeError(f"wsb {args[0]} failed: {text or result.stderr.strip()}") from None


def _running_ids() -> list[str]:
    return [env["Id"] for env in _wsb("list").get("WindowsSandboxEnvironments", [])]


def _kept_id() -> str:
    try:
        return json.loads(STATE.read_text(encoding="utf-8")).get("id", "")
    except (OSError, ValueError):
        return ""


def _stop_kept() -> bool:
    kept = _kept_id()
    if kept and kept in _running_ids():
        _wsb("stop", "--id", kept)
        print(f"sandbox_test: stopped sandbox {kept}.")
        STATE.unlink(missing_ok=True)
        return True
    STATE.unlink(missing_ok=True)
    return False


def _remember(sandbox_id: str, how: str) -> None:
    STATE.write_text(json.dumps({"id": sandbox_id, how: time.strftime("%Y-%m-%dT%H:%M:%S")}),
                     encoding="utf-8")


def _wait_ready(sandbox_id: str, seconds: int = 300) -> bool:
    """Wait until the sandbox accepts commands. They run as SYSTEM: a sandbox started
    with `wsb start` has no logged-in user session until its window is opened."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if _wsb("exec", "--id", sandbox_id, "-r", "System", "-c", "cmd /c exit 0",
                    timeout=60).get("ExitCode") == 0:
                return True
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        time.sleep(5)
    return False


def _guest_has(sandbox_id: str, guest_path: str) -> bool:
    command = f'cmd /c if exist "{guest_path}" (exit 0) else (exit 1)'
    return _wsb("exec", "--id", sandbox_id, "-r", "System", "-c", command).get("ExitCode") == 0


def _adopt(sandbox_id: str) -> None:
    """Re-hydrate a running sandbox this tool doesn't know (stale id, an earlier
    session, or one opened by hand): share the job folder and Git into it."""
    BASE.mkdir(parents=True, exist_ok=True)
    # A marker file proves the guest path shows *this* host folder, not a stale mapping.
    marker = f"host-{os.getpid()}-{int(time.time())}"
    (BASE / marker).write_text("kinetic-devops sandbox job folder\n", encoding="utf-8")
    try:
        if not _guest_has(sandbox_id, rf"{GUEST_BASE}\{marker}"):
            _wsb("share", "--id", sandbox_id, "-f", str(BASE), "-s", GUEST_BASE, "-w")
            if not _guest_has(sandbox_id, rf"{GUEST_BASE}\{marker}"):
                raise RuntimeError(f"could not share {BASE} into sandbox {sandbox_id} as {GUEST_BASE}")
    finally:
        (BASE / marker).unlink(missing_ok=True)
    if not _guest_has(sandbox_id, r"C:\Git\cmd\git.exe") and not _guest_has(sandbox_id, r"C:\kd-git\cmd\git.exe"):
        _wsb("share", "--id", sandbox_id, "-f", str(_git_install_root()), "-s", r"C:\kd-git")
    _remember(sandbox_id, "adopted")


def _ensure_sandbox(fresh: bool) -> str | None:
    """Reuse the sandbox this tool started, adopt one that is already running, or
    start one. Always checks what is running first: only one Windows Sandbox can
    run at a time."""
    if fresh:
        _stop_kept()
        if _running_ids():
            print("sandbox_test: --fresh: a sandbox this tool did not start is still running; "
                  "close it first. Skipped.")
            return None
    running = _running_ids()
    kept = _kept_id()
    if kept and kept in running:
        print(f"sandbox_test: reusing running sandbox {kept}.")
        return kept
    if running:
        sandbox_id = running[0]
        print(f"sandbox_test: adopting running sandbox {sandbox_id} (sharing the job folder into it).")
        if not _wait_ready(sandbox_id, 60):
            print(f"sandbox_test: sandbox {sandbox_id} does not accept commands; close it and retry. Skipped.")
            return None
        _adopt(sandbox_id)
        return sandbox_id

    BASE.mkdir(parents=True, exist_ok=True)
    config = _wsb_config([(BASE, GUEST_BASE, False), (_git_install_root(), r"C:\Git", True)])
    print("sandbox_test: starting a Windows Sandbox (kept running for later runs)...")
    sandbox_id = _wsb("start", "-c", config).get("Id", "")
    if not sandbox_id:
        raise RuntimeError("wsb start did not return a sandbox id")
    _remember(sandbox_id, "started")
    if not _wait_ready(sandbox_id):
        raise RuntimeError(f"sandbox {sandbox_id} started but its session never became ready")
    return sandbox_id


# --- MinGit for the kept sandbox ----------------------------------------------
# Git-heavy tests run git many times; loading git.exe from the host's Git (a
# mapped folder) each time made sandbox timings depend on host load and
# scanning. So the sandbox gets MinGit on its own disk. The host keeps the zip
# in a version-checked cache (downloaded only when missing, outdated, corrupt or
# forced; SHA-256 checked against the release's published digest and on reuse);
# the sandbox reinstalls whenever its installed version differs from the job's.

MINGIT_CACHE = BASE / "cache"
MINGIT_RELEASES = "https://api.github.com/repos/git-for-windows/git/releases/latest"
_MINGIT_ASSET = re.compile(r"^MinGit-(\d+(?:\.\d+)+)-64-bit\.zip$")
_USER_AGENT = "kinetic-devops-sandbox-test"


def _http_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                                   "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def _http_download(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response, dest.open("wb") as fh:
        shutil.copyfileobj(response, fh)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_mingit(refresh: bool = False, cache_dir: Path | None = None,
                  fetch_json=_http_json, download=_http_download) -> dict | None:
    """Return {"zip": Path, "version": str} for a verified MinGit zip in the
    cache, or None (the sandbox then uses the host's Git). refresh=True ignores
    the cache and downloads again."""
    cache = cache_dir or MINGIT_CACHE
    cache.mkdir(parents=True, exist_ok=True)
    meta_path = cache / "mingit.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    cached = None
    if meta.get("zip") and (cache / meta["zip"]).is_file():
        cached_zip = cache / meta["zip"]
        if _sha256(cached_zip) == meta.get("sha256"):
            cached = {"zip": cached_zip, "version": meta.get("version", "")}
        else:
            print(f"sandbox_test: cached {cached_zip.name} failed its checksum; discarding it.")
            cached_zip.unlink(missing_ok=True)

    try:
        release = fetch_json(MINGIT_RELEASES)
        asset = next(a for a in release.get("assets", []) if _MINGIT_ASSET.match(a.get("name", "")))
        version = _MINGIT_ASSET.match(asset["name"]).group(1)
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        if cached:
            print(f"sandbox_test: can't check for a newer MinGit ({exc.__class__.__name__}); "
                  f"using cached {cached['version']}.")
            return cached
        print(f"sandbox_test: MinGit unavailable ({exc.__class__.__name__}: {exc}); "
              "the sandbox uses the host's Git.")
        return None

    if cached and cached["version"] == version and not refresh:
        return cached

    part = cache / (asset["name"] + ".part")
    try:
        download(asset["browser_download_url"], part)
        actual = _sha256(part)
        expected = (asset.get("digest") or "").removeprefix("sha256:")
        if expected and expected != actual:
            raise ValueError(f"checksum mismatch for {asset['name']} (expected {expected[:12]}..., got {actual[:12]}...)")
    except (OSError, ValueError) as exc:
        part.unlink(missing_ok=True)
        if cached:
            print(f"sandbox_test: MinGit {version} download failed ({exc}); using cached {cached['version']}.")
            return cached
        print(f"sandbox_test: MinGit {version} download failed ({exc}); the sandbox uses the host's Git.")
        return None

    zip_path = cache / asset["name"]
    part.replace(zip_path)
    for old in cache.glob("MinGit-*.zip"):  # older versions, and the unversioned name used before
        if old != zip_path:
            old.unlink(missing_ok=True)
    meta_path.write_text(json.dumps({"version": version, "zip": zip_path.name, "sha256": actual,
                                     "source": asset["browser_download_url"]}, indent=2), encoding="utf-8")
    print(f"sandbox_test: cached MinGit {version}.")
    return {"zip": zip_path, "version": version}


# Runs in the sandbox for each job (passed as -EncodedCommand, so no quoting
# issues and no script file on the host). Installs the job's MinGit when the
# sandbox has none, a different version, or a reinstall is forced; then clones
# the job's bundle and runs sandbox_run.ps1. The host's Git (mapped read-only)
# stays on PATH after MinGit, as fallback and for `sh` (git hooks).
_JOB_BOOTSTRAP = r"""
$ErrorActionPreference = 'Continue'
$mingitDir = 'C:\tools\mingit'
$marker = Join-Path $mingitDir 'kinetic-mingit-version.txt'
$zip = '__MINGIT_ZIP__'
$version = '__MINGIT_VERSION__'
$useMingit = $false
if ($zip) {
    $installed = if (Test-Path $marker) { (Get-Content -Raw $marker).Trim() } else { '' }
    if ('__MINGIT_FORCE__' -eq '1' -or $installed -ne $version -or -not (Test-Path "$mingitDir\cmd\git.exe")) {
        try {
            if (Test-Path $mingitDir) { Remove-Item -Recurse -Force $mingitDir }
            Expand-Archive -Force $zip $mingitDir
            Set-Content -Path $marker -Value $version
            Write-Output "MinGit $version installed."
        } catch {
            Write-Output "MinGit install failed, using the host's Git: $_"
        }
    }
    $useMingit = Test-Path "$mingitDir\cmd\git.exe"
}
$hostGit = 'C:\Git\cmd;C:\kd-git\cmd;C:\Git\usr\bin;C:\kd-git\usr\bin'
$env:PATH = $(if ($useMingit) { "$mingitDir\cmd;$hostGit;" } else { "$hostGit;" }) + $env:PATH
# A checkout per job (C:\work\<job name>), so overlapping jobs -- e.g. a
# pre-push check during a manual run -- never delete each other's files.
$checkout = Join-Path 'C:\work' (Split-Path -Leaf '__JOB__')
if (Test-Path $checkout) { Remove-Item -Recurse -Force $checkout }
git init -q $checkout
git -C $checkout fetch -q '__JOB__\repo.bundle' '+refs/kinetic-sandbox/*:refs/kinetic-sandbox/*'
git -C $checkout checkout -q --detach '__SHA__'
if ($LASTEXITCODE -ne 0) { exit 1 }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$checkout\scripts\sandbox_run.ps1" -JobDir '__JOB__'
$code = $LASTEXITCODE
Remove-Item -Recurse -Force $checkout -ErrorAction SilentlyContinue
exit $code
"""


def _job_command(guest_job: str, sha: str, mingit: dict | None = None, force_mingit: bool = False) -> str:
    """The `wsb exec` command for a job. mingit: ensure_mingit()'s result (its
    zip must be in MINGIT_CACHE, which the sandbox sees under GUEST_BASE), or
    None to use the host's Git."""
    guest_zip = rf"{GUEST_BASE}\cache\{mingit['zip'].name}" if mingit else ""
    script = (_JOB_BOOTSTRAP.replace("__JOB__", guest_job).replace("__SHA__", sha)
              .replace("__MINGIT_ZIP__", guest_zip)
              .replace("__MINGIT_VERSION__", mingit["version"] if mingit else "")
              .replace("__MINGIT_FORCE__", "1" if force_mingit else "0"))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"


def run_kept(repo_root: Path, sha: str, options: dict, fresh: bool, timeout_min: int,
             keep_files: bool, mingit_mode: str = "auto") -> tuple[int, Path | None]:
    sandbox_id = _ensure_sandbox(fresh)
    if not sandbox_id:
        return UNAVAILABLE, None
    mingit = None if mingit_mode == "off" else ensure_mingit(refresh=mingit_mode == "refresh")
    # Unique even for jobs started in the same second for the same commit
    # (e.g. a pre-push check overlapping a manual run).
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{sha[:10]}-{os.getpid()}-{secrets.token_hex(2)}"
    job = JOBS / name
    job.mkdir(parents=True)
    # One bundle file instead of a checkout: writing ~1000 files here, where
    # endpoint security scans each one, took ~25 s; the bundle takes ~3 s, and
    # no .ps1 file is written on this machine. The sandbox clones it locally.
    _bundle(repo_root, sha, job / "repo.bundle")
    (job / "options.json").write_text(json.dumps(options), encoding="utf-8")
    guest_job = rf"{GUEST_BASE}\jobs\{name}"
    command = _job_command(guest_job, sha, mingit, force_mingit=mingit_mode == "refresh")
    try:
        _wsb("exec", "--id", sandbox_id, "-r", "System", "-c", command, timeout=timeout_min * 60)
    except subprocess.TimeoutExpired:
        print(f"sandbox_test: no result after {timeout_min} minutes; logs in {job / 'out'}")
        return 1, job
    return (0 if _passed(job / "out") else 1), job


# --- one-shot sandbox (no wsb CLI) -------------------------------------------

def run_one_shot(repo_root: Path, sha: str, options: dict, timeout_min: int) -> tuple[int, Path | None]:
    tasks = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WindowsSandboxRemoteSession.exe", "/NH"],
                           capture_output=True, text=True).stdout
    if "WindowsSandboxRemoteSession" in tasks:
        print("sandbox_test: a Windows Sandbox is already running (only one is allowed); "
              "close it and retry. Skipped.")
        return UNAVAILABLE, None
    work = Path(tempfile.mkdtemp(prefix="kd-sandbox-"))
    in_dir, out_dir = work / "in", work / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    _checkout(repo_root, sha, in_dir / "repo")
    (in_dir / "options.json").write_text(json.dumps(options), encoding="utf-8")
    wsb = work / "kd-sandbox.wsb"
    command = r"powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\kd-in\repo\scripts\sandbox_run.ps1"
    wsb.write_text(_wsb_config([(in_dir, r"C:\kd-in", True), (out_dir, r"C:\kd-out", False),
                                (_git_install_root(), r"C:\Git", True)], command), encoding="utf-8")
    print("sandbox_test: the sandbox window opens; it closes itself when done...")
    subprocess.Popen([str(SANDBOX_EXE), str(wsb)])
    deadline = time.monotonic() + timeout_min * 60
    while not (out_dir / "RESULT").exists() and time.monotonic() < deadline:
        time.sleep(10)
    if not (out_dir / "RESULT").exists():
        print(f"sandbox_test: no result after {timeout_min} minutes; logs in {out_dir}")
        return 1, work
    time.sleep(15)  # let the sandbox shut down and release the mapped folders
    return (0 if _passed(out_dir) else 1), work


def _passed(out_dir: Path) -> bool:
    _print_summary(out_dir)
    result = out_dir / "RESULT"
    return result.exists() and result.read_text(encoding="utf-8", errors="replace").strip().endswith("PASS")


def run(commit: str, suite, smoke: bool, timeout_min: int, keep_open: bool, keep_files: bool,
        fresh: bool, one_shot: bool, junit_out: Path | None = None, mingit_mode: str = "auto") -> int:
    """Exit code as documented; with junit_out, the suite runs via pytest and
    its JUnit XML (per-test timings) is copied to junit_out."""
    if junit_out and suite == "full":
        suite = "pytest-junit"
    if sys.platform != "win32" or not SANDBOX_EXE.is_file():
        print("sandbox_test: Windows Sandbox is not available on this machine; skipped.")
        return UNAVAILABLE
    repo_root = Path(_git("rev-parse", "--show-toplevel"))
    if commit == WORKING_TREE:
        sha = snapshot_working_tree(repo_root)
    else:
        sha = _git("rev-parse", "--verify", f"{commit}^{{commit}}", cwd=repo_root)
    options = {"suite": suite, "smoke": smoke, "keep_open": keep_open}
    print(f"sandbox_test: testing {sha[:12]} ({json.dumps(options)}).")

    started = time.monotonic()
    if _wsb_path() and not one_shot:
        code, work = run_kept(repo_root, sha, options, fresh, timeout_min, keep_files, mingit_mode)
    else:
        code, work = run_one_shot(repo_root, sha, options, timeout_min)
    if code == UNAVAILABLE:
        return code

    if code == 0 and suite in ("full", "pytest-junit") and smoke and commit != WORKING_TREE:
        _record_pass(repo_root, sha)
    if junit_out and work:
        produced = next(iter(sorted(work.glob("**/out/junit.xml"))), None)
        if produced:
            junit_out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(produced, junit_out)
            print(f"sandbox_test: JUnit XML copied to {junit_out}")
            if produced.with_suffix(".json").exists():  # the sandbox's own metadata
                shutil.copyfile(produced.with_suffix(".json"), junit_out.with_suffix(".json"))
    if work and code == 0 and not keep_files:
        shutil.rmtree(work, ignore_errors=True)
    elif work:
        print(f"sandbox_test: job files and logs are in {work}")
    print(f"sandbox_test: {'PASSED' if code == 0 else 'FAILED'} in {time.monotonic() - started:.0f}s")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run tests for a commit on Windows in Windows Sandbox, including the native checks "
            "(.exe launchers, .ps1 scripts) that local runs defer. Default: the full suite plus "
            "the Windows smoke checks, in a sandbox that is kept running and reused. "
            "A running sandbox it doesn't know is adopted. Exit 0 pass, 1 fail, 2 sandbox unavailable."
        )
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--commit", default="HEAD", help="Commit to test (default: HEAD)")
    target.add_argument("--working-tree", action="store_true",
                        help="Test the working tree as it is, including uncommitted and untracked "
                             "(non-ignored) files")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--native-only", action="store_true",
                       help=f"Only the deferred native checks: {' '.join(NATIVE_TESTS)} plus the smoke checks")
    scope.add_argument("--tests", nargs="+", metavar="NAME",
                       help="Run only these unittest names (e.g. tests.test_cli or "
                            "tests.test_cli.TestCli.test_x) instead of the full suite")
    scope.add_argument("--smoke-only", action="store_true",
                       help="Only scripts/windows_smoke.ps1, no Python tests")
    parser.add_argument("--no-smoke", action="store_true", help="Skip scripts/windows_smoke.ps1")
    parser.add_argument("--junit-out", type=Path, metavar="FILE",
                        help="Full suite via pytest; copy its JUnit XML (per-test timings) here "
                             "(for scripts/compare_timings.py)")
    git_source = parser.add_mutually_exclusive_group()
    git_source.add_argument("--refresh-mingit", action="store_true",
                            help="Ignore the cached MinGit: download it again and reinstall it in the sandbox")
    git_source.add_argument("--no-mingit", action="store_true",
                            help="Use the host's Git in the sandbox instead of MinGit")
    parser.add_argument("--fresh", action="store_true",
                        help="Stop the kept sandbox first and start a clean one")
    parser.add_argument("--stop", action="store_true", help="Stop the kept sandbox and exit")
    parser.add_argument("--one-shot", action="store_true",
                        help="Use a new sandbox that closes itself after the run (the default without wsb)")
    parser.add_argument("--timeout", type=int, default=30, metavar="MIN",
                        help="Give up after this many minutes (default: 30)")
    parser.add_argument("--keep-open", action="store_true",
                        help="One-shot mode: leave the sandbox running after the run")
    parser.add_argument("--keep-files", action="store_true",
                        help="Keep the job checkout and logs even when the run passes")
    args = parser.parse_args(argv)

    if args.stop:
        if not _wsb_path():
            parser.error("--stop needs the Windows Sandbox CLI (wsb)")
        if not _stop_kept():
            print("sandbox_test: no kept sandbox is running.")
        return 0
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
    if args.junit_out and suite != "full":
        parser.error("--junit-out needs the full suite (not --native-only/--tests/--smoke-only)")
    return run(WORKING_TREE if args.working_tree else args.commit, suite, smoke, args.timeout, args.keep_open, args.keep_files,
               args.fresh, args.one_shot, args.junit_out,
               "refresh" if args.refresh_mingit else "off" if args.no_mingit else "auto")


if __name__ == "__main__":
    sys.exit(main())
