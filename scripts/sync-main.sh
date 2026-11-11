#!/bin/sh
# sync-main.sh -- after a PR merges upstream: fast-forward the local main and the fork's main to
# public/main, and tag each merged sync/<name> PR as synced/<name> on the fork. Never moves main any
# other way: it stops if either main has a commit that isn't on public/main.
#
# Usage (Git Bash on Windows, or any POSIX shell), from anywhere in the clone:
#   sh scripts/sync-main.sh [--fork REMOTE] [--branch NAME] [--save] [--dry-run] [--no-tags]
# The fork is the remote `origin` and the branch `main` unless given; --save keeps the given
# --fork/--branch in this clone's git config (sync-main.fork, sync-main.branch), e.g. once:
#   sh scripts/sync-main.sh --fork myfork --save
# Details: scripts/sync_main.py.
here="$(cd "$(dirname "$0")" && pwd)" || exit 1   # sync_main.py sits next to this script
cd "$(git rev-parse --show-toplevel)" || exit 1
for py in "${PYTHON:-}" python3 python; do
    if [ -n "$py" ] && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$py" "$here/sync_main.py" "$@"
    fi
done
echo "sync-main: needs Python 3.11+ (set PYTHON=/path/to/python)" >&2
exit 1
