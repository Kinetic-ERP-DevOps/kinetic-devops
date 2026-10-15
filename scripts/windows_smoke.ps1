#Requires -Version 5.1
# windows_smoke.ps1 -- Windows-only checks the Python test suite can't cover:
# console-script launchers, the env_init wrappers and the hook installer.
#
# Run from the repository root after `uv sync` (GitHub's windows-latest job and
# scripts/sandbox_test.py both do). It changes .git/hooks in the checkout it
# runs in, so use a throwaway checkout. Exits 1 if any check fails.

$ErrorActionPreference = "Continue"
$repo = (Get-Location).Path
$failures = New-Object System.Collections.Generic.List[string]

function Check([string]$name, [bool]$ok, [string]$detail = "") {
    if ($ok) {
        Write-Host "[PASS] $name"
    } else {
        Write-Host "[FAIL] $name $detail"
        $failures.Add($name)
    }
}

# Native commands write progress to stderr; with PowerShell 5.1 that must not
# turn into terminating errors, so collect everything as text.
function Run([string]$exe, [string[]]$arguments) {
    $global:LASTEXITCODE = 0
    $text = (& $exe @arguments 2>&1 | ForEach-Object { "$_" }) -join "`n"
    return @{ Code = $LASTEXITCODE; Text = $text }
}

# 1. The package comes from this checkout's src/, via the uv environment.
$r = Run "uv" @("run", "python", "-c", "import kinetic_devops; print(kinetic_devops.__file__)")
$expected = Join-Path $repo "src\kinetic_devops\__init__.py"
Check "kinetic_devops imports from src/" ($r.Code -eq 0 -and $r.Text -like "*$expected*") $r.Text

# 2. Console-script launchers (.exe files uv writes into .venv\Scripts).
foreach ($tool in @("kinetic-devops", "repomaker", "reposmith", "repo-maker")) {
    $exe = Join-Path $repo ".venv\Scripts\$tool.exe"
    if (-not (Test-Path $exe)) {
        Check "$tool launcher exists" $false $exe
        continue
    }
    $r = Run $exe @("--help")
    Check "$tool --help" ($r.Code -eq 0) $r.Text
}

# env_init.py exits with "Could not import 'kinetic_devops'" before doing anything
# else, so either of its later messages is positive evidence the import worked:
# the unknown-environment notice, or the prompt failing on end-of-input (no
# environment is stored here and stdin is empty).
$importedSignal = "Environment 'smoke-env' not recognized|PYTHON CRASH: EOF when reading a line"

# 3. env_init.bat: cmd.exe must parse the whole file (LF-only line endings
#    scramble it). No Kinetic environment is stored here, so it stops at the
#    environment prompt; reaching Python is the success signal.
$r = Run "cmd.exe" @("/c", "scripts\env_init.bat smoke-env < NUL")
$scrambled = $r.Text -match "is not recognized as an internal or external command|was unexpected at this time"
Check "env_init.bat parses" (-not $scrambled -and $r.Text -match "Target:") $r.Text
Check "env_init.bat imports kinetic_devops" ($r.Text -match $importedSignal) $r.Text

# 4. env_init.ps1 (dot-sourced in a child Windows PowerShell 5.1): activates
#    .venv, imports the package without PYTHONPATH.
$probe = @'
$env:PYTHONPATH = ""
try { . .\scripts\env_init.ps1 smoke-env *>&1 | ForEach-Object { "$_" } } catch { "$_" }
"VIRTUAL_ENV=$env:VIRTUAL_ENV"
"PYTHONPATH=$env:PYTHONPATH"
'@
# -EncodedCommand: Windows PowerShell 5.1 mangles double quotes inside native-command arguments.
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($probe))
$r = Run "powershell.exe" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded)
Check "env_init.ps1 activates .venv" ($r.Text -match "VIRTUAL_ENV=.*\.venv") $r.Text
Check "env_init.ps1 imports kinetic_devops" ($r.Text -match $importedSignal) $r.Text
Check "env_init.ps1 leaves PYTHONPATH unset" ($r.Text -match "(?m)^PYTHONPATH=$") $r.Text

# 5. install-hook.ps1 writes a hook that points worktree runs at src/.
$r = Run "powershell.exe" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ".\scripts\install-hook.ps1")
Check "install-hook.ps1 runs" ($r.Code -eq 0) $r.Text
$hook = Join-Path (git rev-parse --path-format=absolute --git-path hooks) "pre-commit"
$hookText = if (Test-Path $hook) { Get-Content -Raw $hook } else { "" }
Check "installed hook uses src/ for KINETIC_HOOK_PYTHON" ($hookText -match [regex]::Escape('PYTHONPATH="$REPO_ROOT/src')) $hook

Write-Host ""
if ($failures.Count -gt 0) {
    Write-Host "windows_smoke: $($failures.Count) check(s) failed: $($failures -join ', ')"
    exit 1
}
Write-Host "windows_smoke: all checks passed"
exit 0
