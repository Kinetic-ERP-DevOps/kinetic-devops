"""scripts/sync-main.sh: main only fast-forwards to public/main; merged sync/<name> PRs get synced/<name> tags."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_MAIN = REPO_ROOT / "scripts" / "sync-main.sh"
INSTALLER = REPO_ROOT / "scripts" / "install-hook.sh"


@unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "git and sh are required")
class TestSyncMain(unittest.TestCase):
    """Each test gets fresh repos: public.git (the upstream, with refs/pull/N/head like GitHub), origin.git
    (the fork) and a clone with the branch guard installed, so pushes go through the real pre-push hook."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "KINETIC_ALLOW_MAIN"}
        self.env.update(KINETIC_PREPUSH_SANDBOX="0", PYTHON=sys.executable,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.git(self.root, "init", "-q", "--bare", "-b", "main", "public.git")
        self.git(self.root, "init", "-q", "--bare", "-b", "main", "origin.git")
        self.git(self.root, "init", "-q", "-b", "main", "up")       # stands in for GitHub merging PRs
        self.up = self.root / "up"
        self.commit(self.up, "chore: seed")
        self.git(self.up, "push", "-q", "../public.git", "main")
        self.git(self.up, "push", "-q", "../origin.git", "main")
        self.git(self.root, "clone", "-q", "-o", "origin", "origin.git", "repo")
        self.repo = self.root / "repo"
        self.git(self.repo, "remote", "add", "public", "../public.git")
        self.git(self.repo, "fetch", "-q", "public")
        self.git(self.repo, "checkout", "-q", "-b", "feat/x")       # work happens on branches
        self.run_ok(["sh", str(INSTALLER)])

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), env=self.env, capture_output=True,
                              text=True, check=True).stdout.strip()

    def commit(self, cwd, msg):
        self.git(cwd, "commit", "-q", "--allow-empty", "-m", msg)
        return self.git(cwd, "rev-parse", "HEAD")

    def merge_pr(self, number, name):
        """Upstream merges PR #number from sync/<name>: its head is kept as refs/pull/N/head, main gets a commit."""
        self.git(self.up, "checkout", "-q", "-b", f"sync/{name}", "main")
        head = self.commit(self.up, f"feat: {name}")
        self.git(self.up, "checkout", "-q", "main")
        merged = self.commit(self.up, f"feat: {name} (#{number})")
        self.git(self.up, "push", "-q", "../public.git", "main", f"{head}:refs/pull/{number}/head")
        return {"number": number, "title": f"feat: {name}", "merged_at": "2026-11-07T00:00:00Z",
                "merge_commit_sha": merged, "head": {"ref": f"sync/{name}", "sha": head}}

    def prs_file(self, prs):
        p = self.root / "prs.json"
        p.write_text(json.dumps(prs), encoding="utf-8")
        return str(p)

    def sync(self, *args):
        return subprocess.run(["sh", str(SYNC_MAIN), *args], cwd=str(self.repo), env=self.env,
                              capture_output=True, text=True)

    def run_ok(self, cmd):
        r = subprocess.run(cmd, cwd=str(self.repo), env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def ref(self, bare, ref):
        return self.git(self.root / bare, "rev-parse", ref)

    def test_fast_forwards_both_mains_and_tags_merged_sync_prs(self):
        prs = [self.merge_pr(7, "feat-a"), self.merge_pr(8, "fix-b"),
               {"number": 9, "title": "bump", "merged_at": "2026-11-07T00:00:00Z", "merge_commit_sha": "0" * 40,
                "head": {"ref": "dependabot/x", "sha": "0" * 40}},                     # not from sync: no tag
               {"number": 10, "title": "closed", "merged_at": None, "merge_commit_sha": None,
                "head": {"ref": "sync/dropped", "sha": "0" * 40}}]                     # closed unmerged: no tag
        r = self.sync("--prs-json", self.prs_file(prs))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        upstream = self.ref("public.git", "main")
        self.assertEqual(self.git(self.repo, "rev-parse", "main"), upstream)
        self.assertEqual(self.ref("origin.git", "main"), upstream)
        self.assertEqual(self.ref("origin.git", "synced/feat-a^{commit}"), prs[0]["head"]["sha"])
        self.assertEqual(self.ref("origin.git", "synced/fix-b^{commit}"), prs[1]["head"]["sha"])
        tags = self.git(self.root / "origin.git", "tag", "-l", "synced/*").split()
        self.assertEqual(sorted(tags), ["synced/feat-a", "synced/fix-b"])
        msg = self.git(self.root / "origin.git", "tag", "-l", "--format=%(contents)", "synced/feat-a")
        self.assertIn("PR #7", msg)
        self.assertIn(prs[0]["merge_commit_sha"][:12], msg)
        self.assertIn("Run workflow dialog", msg)                                   # no sync/feat-a tag
        # Second run: nothing left to do.
        r = self.sync("--prs-json", self.prs_file(prs))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("local main: up to date", r.stdout)
        self.assertIn("synced/ tags: none missing", r.stdout)

    def test_dry_run_changes_nothing(self):
        prs = [self.merge_pr(7, "feat-a")]
        before = (self.git(self.repo, "rev-parse", "main"), self.ref("origin.git", "main"))
        r = self.sync("--dry-run", "--prs-json", self.prs_file(prs))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("would tag synced/feat-a", r.stdout)
        self.assertEqual((self.git(self.repo, "rev-parse", "main"), self.ref("origin.git", "main")), before)
        self.assertEqual(self.git(self.root / "origin.git", "tag", "-l", "synced/*"), "")

    def test_refuses_when_the_fork_main_has_its_own_commit(self):
        self.git(self.root, "clone", "-q", "origin.git", "other")
        self.commit(self.root / "other", "chore: home-only")
        self.git(self.root / "other", "push", "-q", "origin", "main")
        fork_before = self.ref("origin.git", "main")
        self.merge_pr(7, "feat-a")
        r = self.sync("--no-tags")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("origin/main has commits that aren't on public/main", r.stderr)
        self.assertIn("home-only", r.stderr)
        self.assertEqual(self.ref("origin.git", "main"), fork_before)

    def test_refuses_when_local_main_has_its_own_commit(self):
        self.git(self.repo, "checkout", "-q", "main")
        self.git(self.repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty", "-m", "oops")
        self.git(self.repo, "checkout", "-q", "feat/x")
        self.merge_pr(7, "feat-a")
        r = self.sync("--no-tags")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("local main has commits", r.stderr)
        self.assertNotEqual(self.ref("origin.git", "main"), self.ref("public.git", "main"))

    def test_fast_forwards_a_checked_out_main(self):
        self.git(self.repo, "checkout", "-q", "main")
        self.merge_pr(7, "feat-a")
        r = self.sync("--no-tags")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.ref("public.git", "main"))

    def test_never_moves_an_existing_synced_tag(self):
        prs = [self.merge_pr(7, "feat-a")]
        other = self.ref("public.git", "main")
        self.git(self.repo, "fetch", "-q", "public")
        self.git(self.repo, "tag", "-a", "synced/feat-a", other, "-m", "elsewhere")
        self.git(self.repo, "push", "-q", "origin", "refs/tags/synced/feat-a")
        r = self.sync("--prs-json", self.prs_file(prs))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("don't match their PR", r.stderr)
        self.assertEqual(self.ref("origin.git", "synced/feat-a^{commit}"), other)


if __name__ == "__main__":
    unittest.main()
