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
# launcher executables that endpoint security may block. The worktree's src/ is
# put first on PYTHONPATH so its own package code is what gets checked (that
# interpreter's installed copy points at the main checkout).
if [ -n "$KINETIC_HOOK_PYTHON" ]; then
    REPO_ROOT="$(git rev-parse --show-toplevel)"
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) PATH_SEP=";" ;;
        *) PATH_SEP=":" ;;
    esac
    PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+$PATH_SEP$PYTHONPATH}" "$KINETIC_HOOK_PYTHON" $PY_ARGS scripts/hooks/pre-commit
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

# Optional pre-push hook: runs scripts/sandbox_test.py (Windows Sandbox, several
# minutes) for each pushed commit, only when opted in with
#   git config kinetic.prepushSandbox true    (or KINETIC_PREPUSH_SANDBOX=1)
# If the sandbox is unavailable or already running, the push continues with a warning.
# Commits that already passed (scripts/sandbox_test.py records them) are skipped, so
# run the test before pushing, or retry a push whose connection timed out meanwhile.
$prePushContent = @'
#!/bin/sh
#
# This file is managed by scripts/install-hook.ps1. Do not edit directly.

# KINETIC_PREPUSH_SANDBOX=0 skips this check for one push even when enabled;
# =1 runs it once without enabling it. Without Windows Sandbox the check is
# skipped with a warning (sandbox_test.py exit code 2).
case "$KINETIC_PREPUSH_SANDBOX" in
    0) exit 0 ;;
    1) ;;
    *) [ "$(git config --bool kinetic.prepushSandbox 2>/dev/null)" = "true" ] || exit 0 ;;
esac

export UV_LINK_MODE=copy
export PYTHONUTF8=1
# The explicit interpreter is quoted on its own (its path may contain spaces);
# `uv run python` is a multi-word fallback, so they can't share one variable.
run_python() {
    if [ -n "$KINETIC_HOOK_PYTHON" ]; then
        "$KINETIC_HOOK_PYTHON" "$@"
    elif command -v uv >/dev/null 2>&1; then
        uv run python "$@"
    else
        python "$@"
    fi
}

ZERO=0000000000000000000000000000000000000000
TESTED=""
while read -r local_ref local_sha remote_ref remote_sha; do
    [ "$local_sha" = "$ZERO" ] && continue          # branch deletion
    case " $TESTED " in *" $local_sha "*) continue ;; esac
    TESTED="$TESTED $local_sha"
    if grep -qx "$local_sha" "$(git rev-parse --git-path kinetic-sandbox-passed)" 2>/dev/null; then
        echo "pre-push: $local_sha already passed the Windows Sandbox test."
        continue
    fi
    echo "pre-push: Windows Sandbox test of $local_ref ($local_sha)..."
    # Git exports GIT_DIR and friends to hooks; in a linked worktree GIT_DIR is
    # absolute, so git commands the helper runs in its own clones would act on
    # this worktree. Clear them for the helper (like the pre-commit runner does).
    (
        unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_COMMON_DIR \
              GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
        run_python scripts/sandbox_test.py --commit "$local_sha" </dev/null
    )
    status=$?
    if [ $status -eq 2 ]; then
        echo "pre-push: Windows Sandbox test skipped (see above); continuing the push." >&2
    elif [ $status -ne 0 ]; then
        echo "pre-push: Windows Sandbox test failed; push aborted. (Skip once with --no-verify.)" >&2
        exit 1
    fi
done
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
$prePushFile = Join-Path $hooksDir "pre-push"
Set-Content -Path $prePushFile -Value $prePushContent -Encoding Ascii -Force

Write-Host "✅ Successfully installed pre-commit hook to: $hookFile" -ForegroundColor Green
Write-Host "✅ Installed the opt-in pre-push hook to: $prePushFile (enable: git config kinetic.prepushSandbox true)" -ForegroundColor Green
Write-Host "This hook will now run automatically before each commit to prevent issues."