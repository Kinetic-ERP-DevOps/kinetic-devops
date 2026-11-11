#!/bin/sh
#
# branch-guard.sh -- local controls on `main` and the scratch `dev` branch.
#
# Installed next to the git hooks by scripts/install-hook.sh (or .ps1) and called by them:
#   branch-guard.sh commit                       (pre-commit, pre-merge-commit)
#   branch-guard.sh rebase [<branch>]            (pre-rebase; no branch = the current one)
#   branch-guard.sh push <remote> <url> < refs   (pre-push; refs on stdin as git gives them)
#
# Rules (see CONTRIBUTING.md, "Local branch guard"):
#   - No commits (including merge commits) on main, and no rebasing main: main only
#     moves to public/main, via scripts/sync-main.sh. Work on a feature branch, or
#     merge into dev to try branches together. (cherry-pick and am run none of these
#     hooks; pre-push is the backstop for what they put on main.)
#   - main is never pushed to the public remote (it moves there by merging a PR), and
#     is pushed elsewhere only when it is exactly public/main (what sync-main.sh does).
#   - dev is local scratch: never pushed anywhere.
#
# KINETIC_ALLOW_MAIN=1 skips the guard for one command (deliberate repairs only).

[ "$KINETIC_ALLOW_MAIN" = "1" ] && exit 0

refuse() {
    echo "branch-guard: REFUSED: $*" >&2
    echo "branch-guard: (deliberate repair only: KINETIC_ALLOW_MAIN=1 skips this guard once)" >&2
    exit 1
}

case "$1" in
commit)
    branch="$(git symbolic-ref --short -q HEAD)"
    # During a rebase HEAD is detached; the branch being rebased is in head-name
    # ("refs/heads/<name>", or "detached HEAD"). --git-path finds it in worktrees too.
    if [ -z "$branch" ]; then
        for state in rebase-merge rebase-apply; do
            head_name="$(git rev-parse --git-path "$state/head-name")"
            if [ -f "$head_name" ]; then
                branch="$(sed -n '1s|^refs/heads/||p' "$head_name")"
                break
            fi
        done
    fi
    if [ "$branch" = "main" ]; then
        refuse "no commits on main. main moves only to public/main via scripts/sync-main.sh;" \
               "commit on a feature branch, or merge into dev to try branches together."
    fi
    ;;
rebase)
    # pre-rebase gets the branch being rebased as $2, or nothing for the current branch.
    branch="${2#refs/heads/}"
    [ -z "$branch" ] && branch="$(git symbolic-ref -q --short HEAD)"
    if [ "$branch" = "main" ]; then
        refuse "no rebasing main. main moves only to public/main via scripts/sync-main.sh;" \
               "rebase a feature branch instead."
    fi
    ;;
push)
    remote="$2"
    url="$3"
    public_url="$(git remote get-url public 2>/dev/null)"
    to_public=0
    if [ "$remote" = "public" ] || { [ -n "$public_url" ] && [ "$url" = "$public_url" ]; }; then
        to_public=1
    fi
    public_main="$(git rev-parse -q --verify refs/remotes/public/main 2>/dev/null)"
    while read -r local_ref local_sha remote_ref remote_sha; do
        case "$local_ref" in
        refs/heads/dev) refuse "dev is local scratch and is never pushed (to $remote: $remote_ref)." ;;
        esac
        case "$remote_ref" in
        refs/heads/dev) refuse "dev is local scratch; not pushing $local_ref to $remote as dev." ;;
        refs/heads/main)
            if [ "$to_public" = "1" ]; then
                refuse "main is never pushed to the public remote; it moves there by merging a PR."
            fi
            if [ -z "$public_main" ]; then
                refuse "pushing main to $remote needs public/main to compare with (git fetch public)."
            fi
            if [ "$local_sha" != "$public_main" ]; then
                refuse "main goes to $remote only as exactly public/main ($(echo "$public_main" | cut -c1-12));" \
                       "this push is $(echo "$local_sha" | cut -c1-12). Use scripts/sync-main.sh."
            fi
            ;;
        esac
    done
    ;;
*)
    echo "usage: branch-guard.sh commit | rebase [<branch>] | push <remote> <url> < refs" >&2
    exit 2
    ;;
esac
exit 0
