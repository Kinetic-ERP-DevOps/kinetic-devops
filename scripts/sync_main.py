"""sync_main.py -- bring main up to the upstream after a PR merges, and tag what was synced.

Run through scripts/sync-main.sh (any POSIX shell, Git Bash on Windows). Remotes: `public` = the GitHub
upstream (source of truth), `origin` = the maintainer's fork (e.g. a Forgejo mirror). Steps:

  1. fetch public/main and origin/main
  2. stop if origin's main or the local main has a commit that isn't on public/main: main only ever
     fast-forwards to public/main (see CONTRIBUTING.md, "Local branch guard")
  3. fast-forward the local main, then push it to origin (the pre-push guard allows exactly public/main)
  4. for every merged PR from a `sync/<name>` branch without a `synced/<name>` tag on origin: an annotated
     `synced/<name>` tag at the PR head that was merged (fetched as refs/pull/N/head), whose message names the
     PR, the commit it became on main and, if one exists, the `sync/<name>` tag it was promoted from

Options: --dry-run (fetch and report, change nothing), --no-tags (skip step 4), --prs-json FILE (read the
closed PRs from a file instead of the GitHub API, for tests). GITHUB_TOKEN, if set, is sent to the API
(only needed for private repos or the rate limit). Exit 1 = stopped; the reason is printed.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

UP, FORK = "public", "origin"


class Stop(Exception):
    pass


def git(*args, check=True):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if check and r.returncode:
        raise Stop(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def sha(ref):
    return git("rev-parse", "-q", "--verify", ref + "^{commit}", check=False) or None


def is_ancestor(a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b]).returncode == 0


def ahead(base, ref):
    return git("log", "--format=%h %s", f"{base}..{ref}").splitlines()


def remote_tags(prefix):
    """{tag name: commit sha} of the tags on origin under prefix (annotated tags resolved to their commit)."""
    tags = {}
    for line in git("ls-remote", "--tags", FORK, f"refs/tags/{prefix}*").splitlines():
        obj, ref = line.split("\t")
        name = ref[len("refs/tags/"):]
        if name.endswith("^{}"):
            tags[name[:-3]] = obj
        else:
            tags.setdefault(name, obj)
    return tags


def github_repo():
    url = git("remote", "get-url", UP)
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise Stop(f"remote {UP} ({url}) isn't a GitHub repository; use --no-tags or --prs-json")
    return m.group(1)


def closed_prs(prs_json):
    if prs_json:
        with open(prs_json, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(f"https://api.github.com/repos/{github_repo()}/pulls?state=closed&per_page=100",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "sync-main"})
    if os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def sync_main(dry):
    git("fetch", "-q", UP, f"+refs/heads/main:refs/remotes/{UP}/main")
    git("fetch", "-q", FORK, f"+refs/heads/main:refs/remotes/{FORK}/main")
    new = sha(f"refs/remotes/{UP}/main")
    fork = sha(f"refs/remotes/{FORK}/main")
    local = sha("refs/heads/main")
    for who, ref in ((f"{FORK}/main", fork), ("local main", local)):
        if ref and not is_ancestor(ref, new):
            raise Stop(f"{who} has commits that aren't on {UP}/main; main only fast-forwards to {UP}/main. "
                       "Move them to a branch, then reset main to " + f"{UP}/main:\n  " + "\n  ".join(ahead(new, ref)))
    print(f"{UP}/main is at {new[:12]}")
    current = git("symbolic-ref", "-q", "--short", "HEAD", check=False)
    if local == new:
        print("local main: up to date")
    elif dry:
        print(f"local main: would fast-forward {local[:12] if local else '(none)'} -> {new[:12]}")
    elif current == "main":
        git("merge", "-q", "--ff-only", new)
        print(f"local main: fast-forwarded to {new[:12]}")
    else:  # refuses if main is checked out in another worktree
        git("fetch", "-q", ".", f"{new}:refs/heads/main")
        print(f"local main: fast-forwarded to {new[:12]}")
    if fork == new:
        print(f"{FORK}/main: up to date")
    elif dry:
        print(f"{FORK}/main: would push {fork[:12] if fork else '(none)'} -> {new[:12]}")
    else:
        git("push", "-q", FORK, "refs/heads/main:refs/heads/main")
        git("fetch", "-q", FORK, f"+refs/heads/main:refs/remotes/{FORK}/main")
        if sha(f"refs/remotes/{FORK}/main") != new:
            raise Stop(f"{FORK}/main isn't {new[:12]} after the push")
        print(f"{FORK}/main: pushed {new[:12]}")


def tag_synced(dry, prs_json):
    synced, sync_tags = remote_tags("synced/"), remote_tags("sync/")
    todo, mismatched = [], []
    for p in sorted(closed_prs(prs_json), key=lambda p: p["number"]):
        ref = p["head"]["ref"]
        if not p.get("merged_at") or not ref.startswith("sync/"):
            continue
        name, head, n = ref[len("sync/"):], p["head"]["sha"], p["number"]
        tag = f"synced/{name}"
        if tag in synced:
            if synced[tag] != head:
                mismatched.append(f"{tag} is at {synced[tag][:12]}, but PR #{n} merged {head[:12]}")
            continue
        git("fetch", "-q", UP, f"refs/pull/{n}/head")
        if sha("FETCH_HEAD") != head:
            raise Stop(f"PR #{n}: {UP} refs/pull/{n}/head isn't {head[:12]}")
        src = sync_tags.get(f"sync/{name}")
        msg = (f"{ref} -> PR #{n} \"{p['title']}\", merged {p['merged_at'][:10]} as {p['merge_commit_sha'][:12]} on main.\n"
               f"This tag = the PR head that was merged ({head[:12]}).\n"
               + (f"Promoted from {FORK} commit {src[:12]} (tag sync/{name})." if src
                  else "Started from the Run workflow dialog (no sync/ tag)."))
        todo.append((tag, head, msg))
        print(f"{'would tag' if dry else 'tag'} {tag} -> {head[:12]} (PR #{n}, merged as {p['merge_commit_sha'][:12]})")
    if mismatched:  # reported, never moved: a tag records what went up
        raise Stop("synced/ tags that don't match their PR (check by hand; nothing was moved):\n  " + "\n  ".join(mismatched))
    if not todo:
        print("synced/ tags: none missing")
        return
    if dry:
        return
    for tag, head, msg in todo:
        git("tag", "-a", tag, head, "-m", msg)
    git("push", "-q", FORK, *[f"refs/tags/{t}" for t, _, _ in todo])
    after = remote_tags("synced/")
    bad = [t for t, h, _ in todo if after.get(t) != h]
    if bad:
        raise Stop(f"tags not on {FORK} as expected after the push: {', '.join(bad)}")
    print(f"synced/ tags: pushed {len(todo)} to {FORK}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-tags", action="store_true")
    ap.add_argument("--prs-json")
    a = ap.parse_args(argv)
    try:
        for r in (UP, FORK):
            if not git("remote", "get-url", r, check=False):
                raise Stop(f"no remote '{r}' ({UP} = the GitHub upstream, {FORK} = your fork)")
        sync_main(a.dry_run)
        if not a.no_tags:
            tag_synced(a.dry_run, a.prs_json)
    except Stop as e:
        print(f"sync-main: STOPPED: {e}", file=sys.stderr)
        return 1
    print("sync-main: done" + (" (dry run: nothing changed)" if a.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
