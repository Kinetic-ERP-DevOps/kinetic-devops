#!/bin/sh
# install-hook.sh -- install the git hooks from scripts/hooks/templates/ (run once per clone).
#
# Usage (Git Bash on Windows, or any POSIX shell):
#   sh scripts/install-hook.sh
#
# Installs, into the real hooks directory (shared by all worktrees of this clone):
#   pre-commit        branch guard, then the sensitive-data scan and the test suite
#   pre-merge-commit  branch guard (no merge commits on main)
#   pre-push          branch guard (always), then the opt-in Windows Sandbox test
#   branch-guard.sh   the guard the three hooks call
# Re-run after pulling changes to scripts/hooks/templates/. Same result as install-hook.ps1;
# this one needs no PowerShell script execution (which endpoint security may block).
set -eu

repo_root="$(git rev-parse --show-toplevel)"
# --git-path resolves the real hooks directory: in a linked worktree .git is a
# file, not a folder, and core.hooksPath may point somewhere else entirely.
hooks_dir="$(git -C "$repo_root" rev-parse --path-format=absolute --git-path hooks)"
templates="$(cd "$(dirname "$0")" && pwd)/hooks/templates"

mkdir -p "$hooks_dir"
for name in pre-commit pre-merge-commit pre-push branch-guard.sh; do
    # Strip any CR so a CRLF checkout still gives sh-runnable files.
    tr -d '\r' < "$templates/$name" > "$hooks_dir/$name"
    chmod +x "$hooks_dir/$name"
    echo "Installed $hooks_dir/$name"
done
echo "The Windows Sandbox pre-push check stays opt-in: git config kinetic.prepushSandbox true"
