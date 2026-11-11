"""ci-bot (.forgejo/actions/ci-bot/ci_bot.py): a PR must not be able to replace the review script that runs
with LLM_API_KEY and the bot token (review 2026-11-09, finding 1)."""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
CI_BOT = REPO / ".forgejo" / "actions" / "ci-bot" / "ci_bot.py"
REVIEW_REL = Path(".forgejo/actions/ai-review/ai_review.py")

# Stand-ins for ai_review.py: each records which copy ran, from where, and on what checkout.
SCRIPT = """import json, os, subprocess
json.dump({{"who": "{who}", "file": __file__, "cwd": os.getcwd(),
           "head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()}},
          open(os.environ["CI_BOT_TEST_OUT"], "w"))
"""


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


def load_ci_bot(env):
    with patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("ci_bot_under_test", CI_BOT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class TestCiBotReviewScriptIsTrusted(unittest.TestCase):
    def test_pr_cannot_replace_the_review_script(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src, ws = d / "src", d / "ws"
            src.mkdir()
            git(src, "init", "-q", "-b", "main")
            (src / REVIEW_REL).parent.mkdir(parents=True)
            (src / REVIEW_REL).write_text(SCRIPT.format(who="trusted"))
            git(src, "add", "."); git(src, "commit", "-q", "-m", "base")
            base_sha = git(src, "rev-parse", "HEAD").strip()
            git(src, "checkout", "-q", "-b", "pr")
            (src / REVIEW_REL).write_text(SCRIPT.format(who="pr"))   # the PR swaps the script
            git(src, "commit", "-q", "-am", "evil")
            pr_sha = git(src, "rev-parse", "HEAD").strip()
            git(src, "update-ref", "refs/pull/7/head", pr_sha)
            git(src, "checkout", "-q", "main")
            git(d, "clone", "-q", str(src), str(ws))

            out = d / "out.json"
            event = d / "event.json"
            event.write_text(json.dumps({"pull_request": {"number": 7, "requested_reviewers": [{"login": "ci-bot"}]},
                                         "sender": {"login": "someone"}}))
            env = {"FORGEJO_API": "https://forgejo.example/api/v1", "REPO": "o/r", "FORGEJO_TOKEN": "bot-token",
                   "LLM_API_KEY": "llm-key", "LLM_BASE_URL": "https://llm.example/v1", "EVENT_NAME": "pull_request",
                   "GITHUB_EVENT_PATH": str(event), "REVIEW_SCRIPT": str(ws / REVIEW_REL),
                   "CI_BOT_TEST_OUT": str(out)}
            mod = load_ci_bot(env)
            pr_info = {"head": {"ref": "pr", "sha": pr_sha}, "base": {"ref": "main"}}
            cwd = os.getcwd()
            try:
                os.chdir(ws)
                with patch.dict(os.environ, env), patch.object(mod, "api", return_value=pr_info):
                    mod.main()
            finally:
                os.chdir(cwd)

            ran = json.loads(out.read_text())
            self.assertEqual(ran["who"], "trusted", "the PR's own review script ran with the secrets")
            self.assertEqual(ran["head"], pr_sha, "the review must still see the PR head")
            self.assertFalse(Path(ran["file"]).resolve().is_relative_to(ws.resolve()), ran)
            self.assertFalse(Path(ran["cwd"]).resolve().is_relative_to(ws.resolve()), ran)
            # The workspace (which holds ci_bot.py and the review script) never holds the PR's files.
            self.assertEqual(git(ws, "rev-parse", "HEAD").strip(), base_sha)
            self.assertNotIn(str(Path(ran["cwd"]).resolve()), git(ws, "worktree", "list"), "worktree left behind")


if __name__ == "__main__":
    unittest.main()
