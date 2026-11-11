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
from unittest.mock import patch

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


VERSIONS = ["0.9", "1.0.dev1", "1.0a1.dev1", "1.0a1", "1.0a2", "1.0b1", "1.0rc1", "1.0rc2.post1", "1.0", "1.0.0",
            "1.0+local.a", "1.0+local.1", "1.0.post1.dev1", "1.0.post1", "1.0-2", "1.0.1", "1.1", "2.0.0rc1",
            "2.0.0", "2.0.0.post0", "10.0", "1!0.1"]


class TestSyncVersionCompare(unittest.TestCase):
    """Finding 11: pre-release versions crashed the Dependabot check (int compared with str)."""

    def setUp(self):
        ns = sync_defs("vkey", "at_least")
        self.vkey, self.at_least = ns["vkey"], ns["at_least"]

    def test_prerelease_compares(self):
        self.assertTrue(self.vkey("2.0.0") >= self.vkey("2.0.0rc1"))
        self.assertFalse(self.vkey("2.0.0rc1") >= self.vkey("2.0.0"))
        self.assertTrue(self.at_least("2.0.0", "2.0.0rc1"))
        self.assertFalse(self.at_least("1.9.9", "2.0.0rc1"))
        self.assertTrue(self.at_least("1.0", "1.0.0"))

    def test_ordering_matches_pep_440(self):
        for a in VERSIONS:
            for b in VERSIONS:
                with self.subTest(a=a, b=b):
                    ia, ib = VERSIONS.index(a), VERSIONS.index(b)
                    same = {a, b} <= {"1.0", "1.0.0"}
                    self.assertEqual(self.vkey(a) == self.vkey(b), ia == ib or same)
                    if not same and ia != ib:
                        self.assertEqual(self.vkey(a) < self.vkey(b), ia < ib)

    def test_matches_packaging_where_available(self):
        try:
            from packaging.version import Version
        except ImportError:
            self.skipTest("packaging not installed")
        extra = ["1.0alpha1", "1.0-beta.2", "1.0c1", "1.0pre3", "1.0.rev4", "1.0r", "v1.2", "1.0_post2", "1.2.3.dev"]
        vs = VERSIONS + extra
        for a in vs:
            for b in vs:
                with self.subTest(a=a, b=b):
                    self.assertEqual(self.vkey(a) < self.vkey(b), Version(a) < Version(b))
                    self.assertEqual(self.vkey(a) == self.vkey(b), Version(a) == Version(b))

    def test_not_pep_440_is_not_resolved(self):
        with self.assertRaises(ValueError):
            self.vkey("not a version")
        self.assertFalse(self.at_least("2024-01-01-weird", "1.0"))


class FakeResponse:
    def __init__(self, data, link=None):
        self._data, self.headers = json.dumps(data).encode(), {"Link": link} if link else {}

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "status", {}, None)


class TestSyncGuardsRefuseWhenBlind(unittest.TestCase):
    """Finding 3: a guard endpoint that answers 403 (token without the permission) must refuse promotion,
    not read as "nothing found"; and lists are read past the first 100 results."""

    def load(self):
        self.notes, self.problems = [], []
        return sync_defs("GitHubError", "gh", "gh_all", "guard_list",
                         API="https://api.github.com", TOKEN="t", notes=self.notes, problems=self.problems)

    def test_gh_403_is_not_silently_none(self):
        ns = self.load()
        with patch("urllib.request.urlopen", side_effect=http_error(403)):
            with self.assertRaises(SystemExit):
                ns["gh"]("/repos/o/r/security-advisories?state=draft")

    def test_guard_403_refuses_promotion(self):
        ns = self.load()
        for path, ok404 in (("/repos/o/r/security-advisories?state=draft", False),
                            ("/repos/o/r/secret-scanning/alerts?state=open", True)):
            self.problems.clear()
            with patch("urllib.request.urlopen", side_effect=http_error(403)):
                self.assertEqual(ns["guard_list"](path, "something", ok404=ok404), [])
            self.assertEqual(len(self.problems), 1, path)
            self.assertIn("refused", self.problems[0])

    def test_guard_network_failure_refuses_promotion(self):
        ns = self.load()
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            ns["guard_list"]("/repos/o/r/security-advisories?state=draft", "draft security advisories")
        self.assertEqual(len(self.problems), 1)

    def test_secret_scanning_404_means_not_enabled(self):
        ns = self.load()
        with patch("urllib.request.urlopen", side_effect=http_error(404)):
            self.assertIsNone(ns["guard_list"]("/repos/o/r/secret-scanning/alerts?state=open", "x", ok404=True))
        self.assertEqual(self.problems, [])

    def test_informational_403_is_a_note(self):
        ns = self.load()
        with patch("urllib.request.urlopen", side_effect=http_error(403)):
            self.assertIsNone(ns["gh_all"]("/repos/o/r/dependabot/alerts?state=open", ok404=True, ok403=True))
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.problems, [])

    def test_lists_follow_the_link_header_past_100(self):
        ns = self.load()
        api = "https://api.github.com/repos/o/r/security-advisories"
        pages = [FakeResponse([{"ghsa_id": f"GHSA-{i}"} for i in range(100)], f'<{api}?after=c1&per_page=100>; rel="next"'),
                 FakeResponse([{"ghsa_id": "GHSA-last"}], f'<{api}?per_page=100>; rel="first"')]
        seen = []
        with patch("urllib.request.urlopen", side_effect=lambda req, timeout: (seen.append(req.full_url), pages.pop(0))[1]):
            items = ns["guard_list"]("/repos/o/r/security-advisories?state=draft", "draft security advisories")
        self.assertEqual(len(items), 101)
        self.assertEqual(items[-1]["ghsa_id"], "GHSA-last")
        self.assertEqual(seen, [f"{api}?state=draft&per_page=100", f"{api}?after=c1&per_page=100"])
        self.assertEqual(self.problems, [])

    def test_next_page_off_the_api_host_is_refused(self):
        ns = self.load()
        page = FakeResponse([{}] * 100, '<https://elsewhere.example/x?page=2>; rel="next"')
        with patch("urllib.request.urlopen", return_value=page):
            ns["guard_list"]("/repos/o/r/security-advisories?state=draft", "draft security advisories")
        self.assertEqual(len(self.problems), 1)


if __name__ == "__main__":
    unittest.main()
