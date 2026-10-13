#Requires -Version 5.1
# ---------------------------------------------------------------------------
# Git Pre-Commit Hook Installer
#
# This script installs the project's pre-commit hook into your local
# .git/hooks folder. Run this once from within the repository to enable
# automated checks before you commit.
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

# The content of the pre-commit hook script (uses 'sh' for cross-platform compatibility)
# Git for Windows includes a shell environment to run these hooks.
$hookContent = @'
#!/bin/sh
#
# This file is managed by scripts/install-hook.ps1. Do not edit directly.

echo "Running pre-commit checks..."

# Avoid hardlink warnings if the uv cache is on a different drive
export UV_LINK_MODE=copy

# Force UTF-8 for Python processes (fixes UnicodeEncodeError on Windows)
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
PY_ARGS="-X utf8"

# Optional: KINETIC_HOOK_PYTHON names an existing interpreter (e.g. the main
# checkout's .venv python) to use instead of `uv run`. In a worktree without
# its own .venv, `uv run` builds a fresh environment, including new unsigned
# launcher executables that endpoint security may block. The worktree root is
# put first on PYTHONPATH so its own code is what gets checked.
if [ -n "$KINETIC_HOOK_PYTHON" ]; then
    REPO_ROOT="$(git rev-parse --show-toplevel)"
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) PATH_SEP=";" ;;
        *) PATH_SEP=":" ;;
    esac
    PYTHONPATH="$REPO_ROOT${PYTHONPATH:+$PATH_SEP$PYTHONPATH}" "$KINETIC_HOOK_PYTHON" $PY_ARGS scripts/hooks/pre-commit
# Otherwise run the python check script (prefer uv when available)
elif command -v uv >/dev/null 2>&1; then
    uv run python $PY_ARGS scripts/hooks/pre-commit
elif command -v python >/dev/null 2>&1; then
    python $PY_ARGS scripts/hooks/pre-commit
elif command -v py >/dev/null 2>&1; then
    py -3 $PY_ARGS scripts/hooks/pre-commit
else
    echo "Python runtime was not found in PATH. Aborting commit." >&2
    exit 1
fi

EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "Pre-commit checks failed. Aborting commit." >&2
    exit 1
fi

exit 0
'@

# --git-path resolves the real hooks directory: in a linked worktree .git is a
# file, not a folder, and core.hooksPath may point somewhere else entirely.
$hooksDir = git -C $repoRoot rev-parse --path-format=absolute --git-path hooks | Out-String -Stream
if ($LASTEXITCODE -ne 0 -or -not $hooksDir) {
    Write-Host "FATAL: could not resolve the git hooks directory." -ForegroundColor Red
    exit 1
}
$hookFile = Join-Path $hooksDir "pre-commit"

New-Item -ItemType Directory -Path $hooksDir -Force -ErrorAction SilentlyContinue | Out-Null
Set-Content -Path $hookFile -Value $hookContent -Encoding Ascii -Force

Write-Host "✅ Successfully installed pre-commit hook to: $hookFile" -ForegroundColor Green
Write-Host "This hook will now run automatically before each commit to prevent issues."