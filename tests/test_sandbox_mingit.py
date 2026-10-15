"""MinGit cache and job command of scripts/sandbox_test.py, with mocked network.

No network access and no Windows Sandbox needed: the GitHub release lookup and
the download are replaced by fakes, and the cache lives in a temp folder.
"""

import base64
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.script_test_loader import load_script_module

sandbox_test = load_script_module("sandbox_test.py", "sandbox_test_under_test")


def _release(version: str, payload: bytes, digest: str | None = "auto") -> dict:
    name = f"MinGit-{version}-64-bit.zip"
    asset = {"name": name, "browser_download_url": f"https://example.invalid/{name}"}
    if digest == "auto":
        asset["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    elif digest:
        asset["digest"] = digest
    other = {"name": f"MinGit-{version}-busybox-64-bit.zip", "browser_download_url": "https://example.invalid/x"}
    return {"tag_name": f"v{version}.windows.1", "assets": [other, asset]}


class FakeNetwork:
    """Serves one release; counts downloads; can fail either call."""

    def __init__(self, version: str, payload: bytes, digest: str | None = "auto"):
        self.release = _release(version, payload, digest)
        self.payload = payload
        self.downloads = 0
        self.fail_lookup = False
        self.fail_download = False

    def fetch_json(self, url):
        if self.fail_lookup:
            raise OSError("network unreachable")
        return self.release

    def download(self, url, dest: Path):
        self.downloads += 1
        if self.fail_download:
            raise OSError("connection reset")
        dest.write_bytes(self.payload)


class TestEnsureMinGit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)

    def ensure(self, net: FakeNetwork, refresh: bool = False):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = sandbox_test.ensure_mingit(refresh=refresh, cache_dir=self.cache,
                                                fetch_json=net.fetch_json, download=net.download)
        self.output = out.getvalue()
        return result

    def test_empty_cache_downloads_and_verifies(self):
        net = FakeNetwork("2.54.0", b"zip-2.54.0")
        result = self.ensure(net)
        self.assertEqual(result["version"], "2.54.0")
        self.assertEqual(result["zip"].read_bytes(), b"zip-2.54.0")
        self.assertEqual(net.downloads, 1)
        meta = json.loads((self.cache / "mingit.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["sha256"], hashlib.sha256(b"zip-2.54.0").hexdigest())
        self.assertEqual(list(self.cache.glob("*.part")), [])

    def test_current_cache_is_reused_without_download(self):
        net = FakeNetwork("2.54.0", b"zip-2.54.0")
        self.ensure(net)
        result = self.ensure(net)
        self.assertEqual(net.downloads, 1)
        self.assertEqual(result["version"], "2.54.0")

    def test_newer_release_replaces_cached_version(self):
        self.ensure(FakeNetwork("2.54.0", b"old"))
        net = FakeNetwork("2.55.0", b"new")
        result = self.ensure(net)
        self.assertEqual((result["version"], net.downloads), ("2.55.0", 1))
        self.assertEqual(sorted(p.name for p in self.cache.glob("MinGit-*.zip")), ["MinGit-2.55.0-64-bit.zip"])

    def test_refresh_downloads_even_when_current(self):
        net = FakeNetwork("2.54.0", b"zip")
        self.ensure(net)
        self.ensure(net, refresh=True)
        self.assertEqual(net.downloads, 2)

    def test_offline_uses_cached_copy(self):
        net = FakeNetwork("2.54.0", b"zip")
        self.ensure(net)
        net.fail_lookup = True
        result = self.ensure(net)
        self.assertEqual(result["version"], "2.54.0")
        self.assertIn("using cached 2.54.0", self.output)

    def test_offline_without_cache_falls_back_to_host_git(self):
        net = FakeNetwork("2.54.0", b"zip")
        net.fail_lookup = True
        self.assertIsNone(self.ensure(net))
        self.assertIn("host's Git", self.output)

    def test_digest_mismatch_is_rejected_and_nothing_cached(self):
        net = FakeNetwork("2.54.0", b"tampered", digest="sha256:" + "0" * 64)
        self.assertIsNone(self.ensure(net))
        self.assertIn("checksum mismatch", self.output)
        self.assertEqual(list(self.cache.glob("MinGit-*")), [])

    def test_digest_mismatch_keeps_working_cache(self):
        self.ensure(FakeNetwork("2.54.0", b"good"))
        bad = FakeNetwork("2.55.0", b"tampered", digest="sha256:" + "0" * 64)
        result = self.ensure(bad)
        self.assertEqual(result["version"], "2.54.0")

    def test_corrupted_cache_is_discarded_and_redownloaded(self):
        net = FakeNetwork("2.54.0", b"good")
        first = self.ensure(net)
        first["zip"].write_bytes(b"corrupted on disk")
        result = self.ensure(net)
        self.assertEqual(net.downloads, 2)
        self.assertEqual(result["zip"].read_bytes(), b"good")
        self.assertIn("failed its checksum", self.output)

    def test_failed_download_leaves_no_partial_file(self):
        net = FakeNetwork("2.54.0", b"zip")
        net.fail_download = True
        self.assertIsNone(self.ensure(net))
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_release_without_digest_is_accepted(self):
        net = FakeNetwork("2.54.0", b"zip", digest=None)
        self.assertEqual(self.ensure(net)["version"], "2.54.0")


class TestJobCommand(unittest.TestCase):
    def decode(self, command: str) -> str:
        self.assertTrue(command.startswith("powershell.exe "))
        return base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16-le")

    def test_mingit_version_and_force_flag_reach_the_sandbox(self):
        mingit = {"zip": Path("cache/MinGit-2.54.0-64-bit.zip"), "version": "2.54.0"}
        script = self.decode(sandbox_test._job_command(r"C:\kd-jobs\jobs\j1", "abc123", mingit,
                                                       force_mingit=True))
        self.assertIn(r"$zip = 'C:\kd-jobs\cache\MinGit-2.54.0-64-bit.zip'", script)
        self.assertIn("$version = '2.54.0'", script)
        self.assertIn("'1' -eq '1'", script)
        self.assertIn(r"'C:\kd-jobs\jobs\j1\repo.bundle'", script)
        self.assertIn("--detach 'abc123'", script)
        self.assertNotIn("__", script.replace("$env:", ""))  # every placeholder filled

    def test_without_mingit_the_host_git_is_used(self):
        script = self.decode(sandbox_test._job_command(r"C:\kd-jobs\jobs\j1", "abc123", None))
        self.assertIn("$zip = ''", script)
        self.assertIn("'0' -eq '1'", script)


if __name__ == "__main__":
    unittest.main()
