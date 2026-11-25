#Requires -Version 5.1
# ---------------------------------------------------------------------------
# Git Hook Installer (PowerShell)
#
# Installs the hooks from scripts/hooks/templates/ into the real hooks
# directory. Same result as scripts/install-hook.sh, which is the simpler path
# where endpoint security blocks PowerShell scripts: `sh scripts/install-hook.sh`.
#
#   pre-commit        branch guard, then the sensitive-data scan and the test suite
#   pre-merge-commit  branch guard (no merge commits on main)
#   pre-rebase        branch guard (no rebasing main)
#   pre-push          branch guard (always), then the opt-in Windows Sandbox test
#                     (enable: git config kinetic.prepushSandbox true)
#   branch-guard.sh   the guard the four hooks call
# ---------------------------------------------------------------------------

try {
    # Ensure we are running from within a git repository
    $repoRoot = git rev-parse --show-toplevel | Out-String -Stream
    if ($LASTEXITCODE -ne 0) {
        throw "This script must be run from within a git repository."
    }
}
catch {
    Write-Host "FATAL: $_" -ForegroundColor Red
    exit 1
}

# --git-path resolves the real hooks directory: in a linked worktree .git is a
# file, not a folder, and core.hooksPath may point somewhere else entirely.
$hooksDir = git -C $repoRoot rev-parse --path-format=absolute --git-path hooks | Out-String -Stream
if ($LASTEXITCODE -ne 0 -or -not $hooksDir) {
    Write-Host "FATAL: could not resolve the git hooks directory." -ForegroundColor Red
    exit 1
}
$templates = Join-Path $PSScriptRoot "hooks/templates"

New-Item -ItemType Directory -Path $hooksDir -Force -ErrorAction SilentlyContinue | Out-Null
foreach ($name in @("pre-commit", "pre-merge-commit", "pre-rebase", "pre-push", "branch-guard.sh")) {
    # sh needs LF line endings whatever the checkout used.
    $text = [IO.File]::ReadAllText((Join-Path $templates $name)) -replace "`r`n", "`n"
    [IO.File]::WriteAllText((Join-Path $hooksDir $name), $text, (New-Object Text.ASCIIEncoding))
    Write-Host "Installed $(Join-Path $hooksDir $name)" -ForegroundColor Green
}
Write-Host "The Windows Sandbox pre-push check stays opt-in: git config kinetic.prepushSandbox true"
