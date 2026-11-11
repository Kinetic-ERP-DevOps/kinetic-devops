"""Local branch guard: the installed hooks keep main equal to public/main and dev local."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install-hook.sh"
ZERO = "0" * 40


def _clean_env() -> dict:
    # No inherited git state; the sandbox pre-push check stays off whatever the user's config says.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "KINETIC_ALLOW_MAIN"}
    env["KINETIC_PREPUSH_SANDBOX"] = "0"
    return env


@unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "git and sh are required")
class TestBranchGuard(unittest.TestCase):
    """One shared repo (process starts are slow under endpoint security): a few end-to-end checks
    through real git commands, and the push rules by calling the installed guard directly."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.env = _clean_env()
        cls.repo = root / "repo"
        steps = [
            (["git", "init", "-q", "--bare", "-b", "main", "origin.git"], root),
            (["git", "init", "-q", "-b", "main", "repo"], root),
            (["git", "config", "user.email", "t@example.invalid"], cls.repo),
            (["git", "config", "user.name", "t"], cls.repo),
            (["git", "commit", "-q", "--allow-empty", "-m", "chore: seed"], cls.repo),
            (["git", "remote", "add", "origin", "../origin.git"], cls.repo),
            # public is never contacted: the guard only needs its name, URL and public/main
            (["git", "remote", "add", "public", "../public.git"], cls.repo),
            (["git", "update-ref", "refs/remotes/public/main", "main"], cls.repo),
            (["git", "push", "-q", "origin", "main"], cls.repo),
            (["sh", str(INSTALLER)], cls.repo),
            (["git", "checkout", "-q", "-b", "feat/x"], cls.repo),
            (["git", "commit", "-q", "--allow-empty", "--no-verify", "-m", "feat: x"], cls.repo),
            (["git", "checkout", "-q", "main"], cls.repo),
        ]
        for cmd, cwd in steps:
            result = subprocess.run(cmd, cwd=str(cwd), env=cls.env, capture_output=True, text=True)
            if result.returncode != 0:
                cls.tmp.cleanup()
                raise RuntimeError(f"{cmd} failed:\n{result.stdout}\n{result.stderr}")
        cls.main = cls._git_out("rev-parse", "main")
        cls.feat = cls._git_out("rev-parse", "feat/x")
        cls.hooks = cls.repo / ".git" / "hooks"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _git_out(cls, *args):
        return subprocess.run(["git", *args], cwd=str(cls.repo), env=cls.env,
                              capture_output=True, text=True, check=True).stdout.strip()

    def run_in_repo(self, cmd, stdin=None, env=None):
        return subprocess.run(cmd, cwd=str(self.repo), env=env or self.env, input=stdin,
                              capture_output=True, text=True)

    def guard_push(self, remote, local_ref, local_sha, remote_ref, env=None):
        url = "../public.git" if remote == "public" else "../origin.git"
        return self.run_in_repo(["sh", str(self.hooks / "branch-guard.sh"), "push", remote, url],
                                stdin=f"{local_ref} {local_sha} {remote_ref} {ZERO}\n", env=env)

    def assertAllowed(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assertRefused(self, result, text):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(text, result.stderr)

    def test_installs_lf_hooks(self):
        for name in ("pre-commit", "pre-merge-commit", "pre-push", "branch-guard.sh"):
            with self.subTest(hook=name):
                self.assertNotIn(b"\r", (self.hooks / name).read_bytes())

    # --- end to end, through the installed hooks ---

    def test_no_commit_on_main(self):
        result = self.run_in_repo(["git", "commit", "--allow-empty", "-m", "direct"])
        self.assertRefused(result, "no commits on main")

    def test_no_merge_commit_on_main(self):
        result = self.run_in_repo(["git", "merge", "--no-ff", "-m", "merge feat/x", "feat/x"])
        self.run_in_repo(["git", "merge", "--abort"])
        self.assertRefused(result, "no commits on main")
        self.assertEqual(self._git_out("rev-parse", "main"), self.main)

    def test_merges_into_dev_are_allowed(self):
        self.addCleanup(self.run_in_repo, ["git", "checkout", "-q", "main"])
        self.assertAllowed(self.run_in_repo(["git", "checkout", "-q", "-B", "dev", "main"]))
        self.assertAllowed(self.run_in_repo(["git", "merge", "-q", "--no-ff", "-m", "try feat/x", "feat/x"]))

    def test_pre_push_runs_the_guard(self):
        self.assertRefused(self.run_in_repo(["git", "push", "origin", "feat/x:dev"]), "dev is local scratch")
        self.assertAllowed(self.run_in_repo(["git", "push", "-q", "origin", "feat/x"]))

    def test_commit_on_main_during_a_rebase_is_refused(self):
        # A rebase detaches HEAD; the guard reads the branch from rebase-merge/head-name.
        env = {**self.env, "GIT_SEQUENCE_EDITOR": "sh -c 'printf \"break\\n\" > \"$1\"' -"}
        self.addCleanup(self.run_in_repo, ["git", "rebase", "--abort"])
        self.assertAllowed(self.run_in_repo(["git", "rebase", "-i", "HEAD"], env=env))
        self.assertEqual(self.run_in_repo(["git", "symbolic-ref", "-q", "HEAD"]).returncode, 1)
        self.assertRefused(self.run_in_repo(["git", "commit", "--allow-empty", "-m", "during rebase"]),
                           "no commits on main")

    def test_missing_guard_fails_closed(self):
        guard = self.hooks / "branch-guard.sh"
        moved = self.hooks / "branch-guard.sh.moved"
        guard.rename(moved)
        self.addCleanup(moved.rename, guard)
        missing = "branch guard is not installed"
        self.assertRefused(self.run_in_repo(["git", "commit", "--allow-empty", "-m", "unguarded"]), missing)
        self.assertRefused(self.run_in_repo(["git", "push", "origin", "feat/x"]), missing)
        result = self.run_in_repo(["git", "merge", "--no-ff", "-m", "merge feat/x", "feat/x"])
        self.run_in_repo(["git", "merge", "--abort"])
        self.assertRefused(result, missing)
        self.assertIn("install-hook.sh", result.stderr)
        self.assertEqual(self._git_out("rev-parse", "main"), self.main)
        # The escape hatch still works without the guard (pre-push goes on to the sandbox check, off here).
        env = {**self.env, "KINETIC_ALLOW_MAIN": "1"}
        self.assertAllowed(self.run_in_repo(["git", "push", "-q", "origin", "feat/x"], env=env))

    # --- push rules, by calling the guard as pre-push does ---

    def test_main_goes_to_origin_only_as_exactly_public_main(self):
        self.assertAllowed(self.guard_push("origin", "refs/heads/main", self.main, "refs/heads/main"))
        self.assertRefused(self.guard_push("origin", "refs/heads/main", self.feat, "refs/heads/main"),
                           "only as exactly public/main")
        self.assertRefused(self.guard_push("origin", "(delete)", ZERO, "refs/heads/main"),
                           "only as exactly public/main")

    def test_main_is_never_pushed_to_public(self):
        self.assertRefused(self.guard_push("public", "refs/heads/main", self.main, "refs/heads/main"),
                           "never pushed to the public remote")

    def test_dev_is_never_pushed(self):
        self.assertRefused(self.guard_push("origin", "refs/heads/dev", self.feat, "refs/heads/dev"),
                           "dev is local scratch")

    def test_feature_branches_are_not_guarded(self):
        self.assertAllowed(self.guard_push("public", "refs/heads/feat/x", self.feat, "refs/heads/feat/x"))

    def test_allow_main_skips_the_guard_once(self):
        env = {**self.env, "KINETIC_ALLOW_MAIN": "1"}
        self.assertAllowed(self.guard_push("public", "refs/heads/main", self.feat, "refs/heads/main", env=env))
        self.assertAllowed(self.run_in_repo(["sh", str(self.hooks / "branch-guard.sh"), "commit"], env=env))


if __name__ == "__main__":
    unittest.main()
