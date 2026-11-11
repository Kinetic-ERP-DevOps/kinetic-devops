"""Forgejo sync (.forgejo/actions/sync/sync.py): regression tests for the 2026-11-09 review findings.

sync.py acts at import (it is a script run by the workflow), so the tests load only the definitions they need
from its source."""

import ast
import json
import os
import re
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SYNC = REPO / ".forgejo" / "actions" / "sync" / "sync.py"


def sync_defs(*names, **globals_):
    """Top-level functions, classes and assignments named in `names`, from sync.py, without running the script."""
    tree = ast.parse(SYNC.read_text(encoding="utf-8"))

    def wanted(node):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            return node.name in names
        if isinstance(node, ast.Assign):
            return all(isinstance(t, ast.Name) and t.id in names for t in node.targets)
        return False

    keep = [n for n in tree.body if wanted(n)]
    found = {n.name if hasattr(n, "name") else n.targets[0].id for n in keep}
    missing = set(names) - found
    assert not missing, f"not in sync.py: {missing}"
    ns = {"__builtins__": __builtins__, "json": json, "os": os, "re": re, "subprocess": subprocess,
          "urllib": urllib, **globals_}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(SYNC), "exec"), ns)
    return ns


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


class TestSyncTestEnvironment(unittest.TestCase):
    """Finding 2: the promoted branch's tests must not see the GitHub push token or other secrets."""

    def setUp(self):
        self.test_env = sync_defs("TEST_ENV", "TEST_ENV_PREFIXES", "test_env")["test_env"]

    def test_tokens_and_secrets_are_removed(self):
        env = self.test_env({"GH_TOKEN": "ghp_x", "GITHUB_TOKEN": "j", "ACTIONS_RUNTIME_TOKEN": "r",
                             "FORGEJO_TOKEN": "f", "LLM_API_KEY": "k", "UV_PUBLISH_TOKEN": "p",
                             "UV_INDEX_X_PASSWORD": "pw", "GH_REPO": "o/r", "SYNC_PRIVATE_PATTERNS": "x",
                             "RUN_URL": "https://forgejo.example/run/1", "FORGEJO_URL": "https://forgejo.example"})
        self.assertEqual(env, {})

    def test_what_tests_need_is_kept(self):
        base = {"PATH": "/usr/bin", "HOME": "/root", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TMPDIR": "/tmp",
                "UV_CACHE_DIR": "/c", "UV_PYTHON": "3.12", "CI": "true", "https_proxy": "http://proxy.example:3128"}
        self.assertEqual(self.test_env(dict(base, GH_TOKEN="ghp_x")), base)

    def test_the_test_command_gets_the_filtered_environment(self):
        src = SYNC.read_text(encoding="utf-8")
        runs = [line for line in src.splitlines() if '"bash", "-c", cmd' in line]
        self.assertEqual(len(runs), 1, runs)
        self.assertIn("env=test_env(E)", runs[0])


if __name__ == "__main__":
    unittest.main()
