#Requires -Version 5.1
# sandbox_run.ps1 -- runs INSIDE Windows Sandbox; started by scripts/sandbox_test.py.
#
# Mapped folders: C:\kd-in (read-only: repo checkout of the commit under test and
# options.json), C:\kd-out (writable: logs + RESULT), C:\Git (read-only: the
# host's Git install). Installs uv, syncs, then runs what options.json asks for:
#   suite: "full" (tests.test_runner), "none", or a list of unittest names
#   smoke: true/false (scripts/windows_smoke.ps1)
# with native checks enabled. Writes C:\kd-out\RESULT (PASS/FAIL), then shuts the
# sandbox down unless options.json has "keep_open": true.

$ErrorActionPreference = "Continue"
$out = "C:\kd-out"
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
$env:PATH = "C:\Git\cmd;C:\Git\usr\bin;" + $env:PATH
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# The sandbox has no endpoint-security agent: run the native (.exe launcher) checks.
$env:KINETIC_NATIVE_CHECKS = "1"
$options = Get-Content -Raw C:\kd-in\options.json | ConvertFrom-Json
Add-Content -Path $summary -Value ("Options: " + ($options | ConvertTo-Json -Compress))

Step "install uv" { powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" }
$env:PATH = "$env:USERPROFILE\.local\bin;" + $env:PATH

# Writable copy: uv sync creates .venv inside the checkout.
$repo = "C:\work\kd"
Step "copy checkout" { New-Item -ItemType Directory -Force C:\work | Out-Null; Copy-Item -Recurse -Force C:\kd-in\repo $repo }
Set-Location $repo
git config --global --add safe.directory '*'
git config --global user.name "sandbox"
git config --global user.email "sandbox@example.invalid"

Step "uv sync" { uv sync --locked }
if ($options.suite -is [string]) {
    if ($options.suite -eq "full") { Step "test suite" { uv run python -m tests.test_runner --native } }
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

if (-not $options.keep_open) {
    shutdown.exe /s /t 5
}
