#Requires -Version 5.1
# sandbox_run.ps1 -- runs INSIDE Windows Sandbox; started by scripts/sandbox_test.py.
#
# Two modes:
# - Job mode (-JobDir, used with a long-lived sandbox via `wsb exec`): the job
#   folder holds repo.bundle, options.json and out\; sandbox_test.py clones the
#   bundle to a checkout of its own (C:\work\<job name>) and runs this script
#   from there.
# - One-shot mode (no -JobDir, started as the sandbox's logon command):
#   C:\kd-in holds repo\ and options.json, C:\kd-out receives the logs.
# C:\Git (C:\kd-git in an adopted sandbox) is the host's Git install (read-only). Installs uv if missing, syncs
# into a fresh copy of the checkout, then runs what options.json asks for:
#   suite: "full" (tests.test_runner), "pytest-junit" (same tests via pytest,
#          writing out\junit.xml), "none", or a list of unittest names
#   smoke: true/false (scripts/windows_smoke.ps1)
# with native checks enabled, and writes out\RESULT (PASS/FAIL). One-shot mode
# then shuts the sandbox down unless options.json has "keep_open": true.
param([string]$JobDir = "")

$ErrorActionPreference = "Continue"
if ($JobDir) {
    $in = $JobDir
    $out = Join-Path $JobDir "out"
} else {
    $in = "C:\kd-in"
    $out = "C:\kd-out"
}
New-Item -ItemType Directory -Force $out | Out-Null
$summary = Join-Path $out "summary.txt"
$failed = $false

function Step([string]$name, [scriptblock]$body) {
    $log = Join-Path $out ("step-" + ($name -replace '[^A-Za-z0-9]+', '-') + ".log")
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $global:LASTEXITCODE = 0
    try {
        & $body 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $log -Encoding utf8
        $code = $LASTEXITCODE
    } catch {
        "$_" | Out-File -FilePath $log -Append -Encoding utf8
        $code = "EXCEPTION"
    }
    if ($code -ne 0) { $script:failed = $true }
    Add-Content -Path $summary -Value ("{0,-28} exit={1,-10} {2,6:N1}s" -f $name, $code, $timer.Elapsed.TotalSeconds)
}

Set-Content -Path $summary -Value ("Windows Sandbox run started " + (Get-Date -Format s))
# Git: in job mode the bootstrap in sandbox_test.py already put the chosen git
# first on PATH (MinGit on the sandbox's own disk, or the host's Git with
# --no-mingit). Append the host's Git -- C:\Git when mapped at start, C:\kd-git
# when shared into an adopted sandbox -- as fallback and for `sh` (git hooks);
# in one-shot mode it is the only git.
$env:PATH = $env:PATH + ";C:\Git\cmd;C:\kd-git\cmd;C:\Git\usr\bin;C:\kd-git\usr\bin"
Add-Content -Path $summary -Value ("git: " + (Get-Command git -ErrorAction SilentlyContinue).Source)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# The sandbox has no endpoint-security agent: run the native (.exe launcher) checks.
$env:KINETIC_NATIVE_CHECKS = "1"
$options = Get-Content -Raw (Join-Path $in "options.json") | ConvertFrom-Json
Add-Content -Path $summary -Value ("Options: " + ($options | ConvertTo-Json -Compress))

$env:PATH = "$env:USERPROFILE\.local\bin;" + $env:PATH
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Step "install uv" { powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" }
}

# Fresh writable checkout per run (uv sync creates .venv inside it); the uv
# cache stays warm between runs in a long-lived sandbox.
if ($JobDir) {
    # Job mode: sandbox_test.py cloned the job's repo.bundle into a checkout of
    # its own (C:\work\<job name>) and runs this script from it.
    $repo = Split-Path -Parent $PSScriptRoot
} else {
    # One-shot mode: the host provided a checkout; copy it.
    $repo = "C:\work\kd"
    if (Test-Path $repo) { Remove-Item -Recurse -Force $repo }
    Step "copy checkout" { New-Item -ItemType Directory -Force C:\work | Out-Null; Copy-Item -Recurse -Force (Join-Path $in "repo") $repo }
}
Set-Location $repo
git config --global --add safe.directory '*'
git config --global user.name "sandbox"
git config --global user.email "sandbox@example.invalid"

Step "uv sync" { uv sync --locked }
if ($options.suite -is [string]) {
    if ($options.suite -eq "full") { Step "test suite" { uv run python -m tests.test_runner --native } }
    # Same tests via pytest, with per-test timings for scripts/compare_timings.py.
    if ($options.suite -eq "pytest-junit") {
        $junit = Join-Path $out "junit.xml"
        Step "test suite (pytest)" { uv run python -m pytest -q -p no:cacheprovider "--junitxml=$junit" }
        # This environment's own Python/OS for the timing record; the host adds end-to-end time.
        uv run python scripts/compare_timings.py meta --label windows-sandbox --out (Join-Path $out "junit.json") --extra "native checks" 2>&1 | Out-Null
    }
} else {
    $names = @($options.suite)
    Step "selected tests" { uv run python -m unittest -v @names }
}
if ($options.smoke) {
    Step "windows smoke" { powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows_smoke.ps1 }
}

$result = if ($failed) { "FAIL" } else { "PASS" }
Add-Content -Path $summary -Value ("Finished " + (Get-Date -Format s) + ": $result")
Set-Content -Path (Join-Path $out "RESULT") -Value $result

if (-not $JobDir -and -not $options.keep_open) {
    shutdown.exe /s /t 5
}
