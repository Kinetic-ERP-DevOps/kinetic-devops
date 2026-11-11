"""Tests for kinetic_devops.idp (Epicor IdP sign-in) and its use in auth. No network: requests is mocked."""

import base64
import copy
import contextlib
import hashlib
import io
import json
import os
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import MagicMock, patch

from kinetic_devops import auth, crypto, idp
from kinetic_devops.auth import KineticConfigManager, SessionResolutionError
from tests.cli_matrix import native_checks_enabled

CONFIG = {"IdentityProvider": {"Enabled": True, "Endpoint": "https://idp.example/tid/42/",
                               "ApiScope": "epicor_erp idp_access"}}
DISCOVERY = {"issuer": "https://idp.example/tid/42",
             "authorization_endpoint": "https://idp.example/tid/42/connect/authorize",
             "token_endpoint": "https://idp.example/tid/42/connect/token"}
SETTINGS = {"endpoint": "https://idp.example/tid/42", "issuer": "https://idp.example/tid/42", "tenant": "42",
            "authorize": DISCOVERY["authorization_endpoint"], "token": DISCOVERY["token_endpoint"],
            "api_scope": "epicor_erp idp_access"}


def _resp(json_body, status=200, url="https://example.invalid/", history=()):
    r = MagicMock()
    r.ok = 200 <= status < 300
    r.status_code = status
    r.json.return_value = json_body
    r.headers = {"content-type": "application/json"}
    r.url = url
    r.history = list(history)
    return r


def _id_token(**claims):
    """An unsigned JWT with these claims (signatures aren't checked: the token comes from the token endpoint)."""
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


def _claims(nonce, **over):
    return {"iss": SETTINGS["issuer"], "aud": "cid", "exp": time.time() + 300, "nonce": nonce, "sub": "s-1",
            "preferred_username": "alice@corp.example", **over}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestDiscover(unittest.TestCase):
    def _discover(self, server="https://kinetic.example/Env/", config=CONFIG, discovery=DISCOVERY, **resp_kw):
        req = MagicMock()
        req.get.side_effect = lambda url, **kw: _resp(config if url.endswith("/api/.configuration") else discovery,
                                                      url=resp_kw.get("url", url), **{k: v for k, v in resp_kw.items() if k != "url"})
        with patch.object(idp, "requests", req):
            return idp.discover(server), req

    def test_reads_tenant_endpoints_and_scope(self):
        s, req = self._discover()
        self.assertEqual(s, SETTINGS)
        self.assertEqual(req.get.call_args_list[0].args[0], "https://kinetic.example/Env/api/.configuration")
        self.assertTrue(all(c.kwargs.get("verify") is True for c in req.get.call_args_list))

    def test_http_server_url_is_refused(self):
        with self.assertRaises(idp.IdPError):
            self._discover(server="http://kinetic.example/Env")

    def test_http_idp_endpoint_is_refused(self):
        with self.assertRaises(idp.IdPError):
            self._discover(config={"IdentityProvider": {"Enabled": True, "Endpoint": "http://idp.example/tid/42"}})

    def test_redirect_to_http_is_refused_before_it_is_requested(self):
        req = MagicMock()
        moved = _resp({}, 302)
        moved.headers = {"Location": "http://elsewhere.example/api/.configuration"}
        req.get.return_value = moved
        with patch.object(idp, "requests", req), self.assertRaises(idp.IdPError) as ctx:
            idp.discover("https://kinetic.example/Env")
        self.assertIn("redirect target", str(ctx.exception))
        req.get.assert_called_once()   # the http address was never requested
        self.assertIs(req.get.call_args.kwargs["allow_redirects"], False)

    def test_https_redirect_is_followed(self):
        moved = _resp({}, 302)
        moved.headers = {"Location": "/Env2/api/.configuration"}
        req = MagicMock()
        req.get.side_effect = [moved, _resp(CONFIG), _resp(DISCOVERY)]
        with patch.object(idp, "requests", req):
            s = idp.discover("https://kinetic.example/Env")
        self.assertEqual(s["issuer"], SETTINGS["issuer"])
        self.assertEqual(req.get.call_args_list[1].args[0], "https://kinetic.example/Env2/api/.configuration")

    def test_redirect_loop_is_refused(self):
        moved = _resp({}, 302)
        moved.headers = {"Location": "https://kinetic.example/Env/api/.configuration"}
        req = MagicMock()
        req.get.return_value = moved
        with patch.object(idp, "requests", req), self.assertRaises(idp.IdPError):
            idp.discover("https://kinetic.example/Env")

    def test_error_status_is_refused(self):
        with self.assertRaises(idp.IdPError):
            self._discover(status=500)

    def test_issuer_must_be_the_endpoint(self):
        with self.assertRaises(idp.IdPError) as ctx:
            self._discover(discovery={**DISCOVERY, "issuer": "https://evil.example/tid/42"})
        self.assertIn("issuer", str(ctx.exception))

    def test_issuer_trailing_slash_and_host_case_are_the_same(self):
        s, _ = self._discover(discovery={**DISCOVERY, "issuer": "https://IDP.example/tid/42/"})
        self.assertEqual(s["issuer"], "https://IDP.example/tid/42/")

    def test_token_endpoint_on_another_host_is_refused(self):
        with self.assertRaises(idp.IdPError):
            self._discover(discovery={**DISCOVERY, "token_endpoint": "https://evil.example/connect/token"})

    def test_http_authorize_endpoint_is_refused(self):
        with self.assertRaises(idp.IdPError):
            self._discover(discovery={**DISCOVERY, "authorization_endpoint": "http://idp.example/tid/42/connect/authorize"})

    def test_refresh_refuses_a_stored_http_token_endpoint(self):
        req = MagicMock()
        with patch.object(idp, "requests", req), self.assertRaises(idp.IdPError):
            idp.refresh("http://idp.example/tid/42/connect/token", "cid", "RT")
        req.post.assert_not_called()

    def test_disabled_idp_raises(self):
        req = MagicMock()
        req.get.return_value = _resp({"IdentityProvider": {"Enabled": False}})
        with patch.object(idp, "requests", req), self.assertRaises(idp.IdPError):
            idp.discover("https://kinetic.example/Env")


def _get(url, headers=None):
    """GET from the test 'browser': (status, headers); 0 if the listener is already gone."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=5) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)
    except OSError:
        return 0, {}


class TestLoginBrowser(unittest.TestCase):
    def _run(self, reply, timeout=10, claims=None, **kw):
        """reply(params) -> list of (query dict, extra headers) the 'browser' sends to the loopback redirect, in order."""
        port = _free_port()
        self.statuses = []

        def browser(url):
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            self.seen = params

            def go():
                time.sleep(0.2)
                for query, headers in reply(params):
                    self.statuses.append(_get(f"{params['redirect_uri']}?{urllib.parse.urlencode(query)}", headers))
            threading.Thread(target=go, daemon=True).start()
            return True

        req = MagicMock()
        req.post.side_effect = lambda *a, **k: _resp({
            "access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
            "id_token": _id_token(**(claims(self.seen["nonce"]) if claims else _claims(self.seen["nonce"])))})
        with patch.object(idp, "requests", req):
            tok = idp.login_browser(SETTINGS, "cid", port=port, timeout=timeout, open_url=browser, **kw)
        return tok, req, port

    def test_pkce_code_exchange(self):
        tok, req, port = self._run(lambda p: [({"code": "C1", "state": p["state"]}, {})])
        self.assertEqual(tok["access_token"], "AT")
        self.assertEqual(self.seen["code_challenge_method"], "S256")
        self.assertEqual(self.seen["redirect_uri"], f"http://127.0.0.1:{port}/callback")
        self.assertIn("offline_access", self.seen["scope"])
        self.assertIn("epicor_erp", self.seen["scope"])
        data = req.post.call_args.kwargs["data"]
        self.assertEqual((data["grant_type"], data["code"], data["client_id"]), ("authorization_code", "C1", "cid"))
        self.assertTrue(data["code_verifier"])
        status, headers = self.statuses[0]
        self.assertEqual(status, 200)
        self.assertEqual((headers.get("Cache-Control"), headers.get("Referrer-Policy")), ("no-store", "no-referrer"))

    def test_force_login_asks_the_idp_for_a_new_sign_in(self):
        self._run(lambda p: [({"code": "C1", "state": p["state"]}, {})], force_login=True)
        self.assertEqual(self.seen["prompt"], "login")

    def test_default_reuses_the_browsers_idp_session(self):
        self._run(lambda p: [({"code": "C1", "state": p["state"]}, {})])
        self.assertNotIn("prompt", self.seen)

    def test_wrong_state_is_ignored_and_the_real_callback_still_wins(self):
        tok, req, _ = self._run(lambda p: [({"code": "EVIL", "state": "forged"}, {}),
                                           ({"error": "access_denied"}, {}),   # no state at all
                                           ({"code": "C1", "state": p["state"]}, {})])
        self.assertEqual([s for s, _ in self.statuses[:2]], [400, 400])
        self.assertEqual(req.post.call_args.kwargs["data"]["code"], "C1")

    def test_only_wrong_state_callbacks_time_out(self):
        with self.assertRaises(idp.IdPError) as ctx:
            self._run(lambda p: [({"code": "C1", "state": "forged"}, {})], timeout=2)
        self.assertIn("No sign-in came back", str(ctx.exception))
        self.assertEqual(self.statuses[0][0], 400)

    def test_second_callback_is_refused(self):
        tok, req, _ = self._run(lambda p: [({"code": "C1", "state": p["state"]}, {}),
                                           ({"code": "C2", "state": p["state"]}, {})])
        self.assertEqual(self.statuses[0][0], 200)
        self.assertNotEqual(self.statuses[1][0], 200)   # 400, or 0 if the listener has already closed
        self.assertEqual(req.post.call_args.kwargs["data"]["code"], "C1")

    def test_other_host_header_is_refused(self):
        tok, req, port = self._run(lambda p: [({"code": "EVIL", "state": p["state"]}, {"Host": "attacker.example"}),
                                              ({"code": "C1", "state": p["state"]}, {})])
        self.assertEqual(self.statuses[0][0], 400)
        self.assertEqual(req.post.call_args.kwargs["data"]["code"], "C1")

    def test_iss_from_another_issuer_is_refused(self):
        with self.assertRaises(idp.IdPError) as ctx:
            self._run(lambda p: [({"code": "C1", "state": p["state"], "iss": "https://evil.example"}, {})])
        self.assertIn("RFC 9207", str(ctx.exception))

    def test_matching_iss_is_accepted(self):
        tok, _, _ = self._run(lambda p: [({"code": "C1", "state": p["state"], "iss": SETTINGS["issuer"]}, {})])
        self.assertEqual(tok["access_token"], "AT")

    def test_idp_error_is_reported(self):
        with self.assertRaises(idp.IdPError) as ctx:
            self._run(lambda p: [({"error": "access_denied", "state": p["state"]}, {})])
        self.assertIn("access_denied", str(ctx.exception))

    def test_nonce_is_sent_and_identity_returned(self):
        tok, _, _ = self._run(lambda p: [({"code": "C1", "state": p["state"]}, {})])
        self.assertGreaterEqual(len(self.seen["nonce"]), 16)
        self.assertEqual(tok["idp_identity"], {"sub": "s-1", "username": "alice@corp.example"})

    def test_id_token_for_another_sign_in_is_refused(self):
        with self.assertRaises(idp.IdPError) as ctx:
            self._run(lambda p: [({"code": "C1", "state": p["state"]}, {})], claims=lambda n: _claims("other-nonce"))
        self.assertIn("nonce", str(ctx.exception))


class TestIdToken(unittest.TestCase):
    """check_id_token: OIDC Core 3.1.3.7 claim checks on the ID token from the code exchange."""

    def check(self, **over):
        return idp.check_id_token({"id_token": _id_token(**{**_claims("N"), **over})}, SETTINGS["issuer"], "cid", "N")

    def refused(self, word, **over):
        with self.assertRaises(idp.IdPError) as ctx:
            self.check(**over)
        self.assertIn(word, str(ctx.exception))

    def test_valid_token_returns_claims(self):
        self.assertEqual(self.check()["sub"], "s-1")

    def test_missing_id_token_is_refused(self):
        with self.assertRaises(idp.IdPError):
            idp.check_id_token({"access_token": "AT"}, SETTINGS["issuer"], "cid", "N")

    def test_unreadable_id_token_is_refused(self):
        with self.assertRaises(idp.IdPError):
            idp.check_id_token({"id_token": "not-a-jwt"}, SETTINGS["issuer"], "cid", "N")

    def test_other_issuer_is_refused(self):
        self.refused("issued by", iss="https://evil.example/tid/42")

    def test_other_audience_is_refused(self):
        self.refused("aud", aud="someone-else")

    def test_several_audiences_need_azp(self):
        self.refused("azp", aud=["cid", "api"])
        self.assertEqual(self.check(aud=["cid", "api"], azp="cid")["sub"], "s-1")

    def test_azp_for_another_party_is_refused(self):
        self.refused("azp", azp="someone-else")

    def test_expired_token_is_refused(self):
        self.refused("expired", exp=time.time() - 3600)

    def test_small_clock_skew_is_allowed(self):
        self.assertTrue(self.check(exp=time.time() - 30))

    def test_wrong_nonce_is_refused(self):
        self.refused("nonce", nonce="other")

    def test_identity_name_and_match(self):
        self.assertEqual(idp.identity_name({"email": "jdoe@corp.example", "name": "J Doe"}), "jdoe@corp.example")
        self.assertTrue(idp.identity_matches("JDoe@corp.example", "jdoe"))
        self.assertTrue(idp.identity_matches("CORP\\jdoe", "jdoe"))
        self.assertFalse(idp.identity_matches("bsmith@corp.example", "jdoe"))
        self.assertTrue(idp.identity_matches("", "jdoe"))   # nothing to compare: no warning

    def test_session_meta_stores_the_identity_not_the_id_token(self):
        meta = idp.session_meta({"access_token": "AT", "id_token": "x.y.z",
                                 "idp_identity": {"sub": "s-1", "username": "alice@corp.example"}},
                                SETTINGS, "Alice", "pilot", "ACME")
        self.assertEqual((meta["idp_subject"], meta["idp_username"], meta["user_id"]), ("s-1", "alice@corp.example", "alice"))
        self.assertNotIn("id_token", meta)


class TestRefreshSession(unittest.TestCase):
    def setUp(self):
        self.meta = {"AccessToken": "OLD", "refresh_token": "RT1", "idp_tenant": "42",
                     "idp_token_endpoint": SETTINGS["token"], "_is_valid": False}
        self.stored = []
        self.slot = f"test-slot-{time.time()}"

    def _refresh(self, reread, force=False, post=None):
        req = MagicMock()
        req.post.return_value = post or _resp({"access_token": "NEW", "refresh_token": "RT2", "expires_in": 3600})
        with patch.object(idp, "requests", req), patch.object(idp, "get_client_id", return_value="cid"):
            tok = idp.refresh_session(self.meta, reread=reread, store=self.stored.append, slot=self.slot, force=force)
        return tok, req

    def test_rotates_and_stores_new_refresh_token(self):
        tok, req = self._refresh(lambda: dict(self.meta))
        self.assertEqual(tok, "NEW")
        self.assertEqual(req.post.call_args.kwargs["data"]["refresh_token"], "RT1")
        self.assertEqual((req.post.call_args.kwargs["verify"], req.post.call_args.kwargs["allow_redirects"]), (True, False))
        self.assertEqual(self.stored[-1]["refresh_token"], "RT2")
        self.assertNotIn("_is_valid", self.stored[-1])

    def test_another_process_already_refreshed(self):
        tok, req = self._refresh(lambda: {**self.meta, "AccessToken": "OTHER", "_is_valid": True})
        self.assertEqual(tok, "OTHER")
        req.post.assert_not_called()

    def test_force_refreshes_a_rejected_token_still_in_its_lifetime(self):
        tok, req = self._refresh(lambda: {**self.meta, "_is_valid": True}, force=True)
        self.assertEqual(tok, "NEW")
        req.post.assert_called_once()

    def test_rejected_refresh_token_is_dropped(self):
        tok, _ = self._refresh(lambda: dict(self.meta), post=_resp({"error": "invalid_grant"}, 400))
        self.assertIsNone(tok)
        self.assertEqual(self.stored[-1]["refresh_token"], "")
        self.assertEqual(self.stored[-1]["AccessToken"], "OLD")
        self.assertNotIn("_is_valid", self.stored[-1])

    def test_temporary_refresh_failure_keeps_the_refresh_token(self):
        tok, _ = self._refresh(lambda: dict(self.meta), post=_resp({"error": "temporarily_unavailable"}, 503))
        self.assertIsNone(tok)
        self.assertEqual(self.stored, [])

    def test_network_error_takes_the_sign_in_again_path(self):
        """Review finding #9: a ConnectionError/Timeout during a refresh was a traceback, not "sign in again"."""
        import requests
        for exc in (requests.ConnectionError("network down"), requests.Timeout("slow")):
            with patch("requests.post", side_effect=exc), patch.object(idp, "get_client_id", return_value="cid"):
                tok = idp.refresh_session(self.meta, reread=lambda: dict(self.meta), store=self.stored.append,
                                          slot=self.slot)
            self.assertIsNone(tok)
        self.assertEqual(self.stored, [])   # the refresh token is kept: the failure may be temporary

    def test_malformed_json_is_an_idp_error(self):
        bad = _resp({}, 200)
        bad.json.side_effect = ValueError("Expecting value")
        req = MagicMock()
        req.post.return_value = bad
        with patch.object(idp, "requests", req), self.assertRaises(idp.IdPError) as ctx:
            idp.refresh(SETTINGS["token"], "cid", "RT")
        self.assertIn("malformed JSON", str(ctx.exception))

    def _lock_path(self):
        return os.path.join(idp.lock_dir(), "kinetic-idp-" + hashlib.sha256(self.slot.encode()).hexdigest()[:16] + ".lock")

    def _try(self, wait=0.5):
        """Try the slot lock in a thread: 'locked' or the IdPError text; fails if it spins past its deadline."""
        outcome = []

        def worker():
            try:
                with idp.slot_lock(self.slot, wait=wait):
                    outcome.append("locked")
            except idp.IdPError as exc:
                outcome.append(str(exc))
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(wait + 5)
        self.assertFalse(t.is_alive(), "slot_lock spun past its deadline")
        return outcome[0]

    def test_lock_file_that_cant_be_opened_is_reported_at_once(self):
        """An open error (permissions, a full disk) is not "another process is refreshing": it is reported at once
        with its reason, instead of being waited out for 30 s (or, before that, retried at once forever)."""
        import errno
        for exc in (PermissionError(errno.EACCES, "Permission denied"), OSError(errno.ENOSPC, "No space left")):
            with patch.object(idp.os, "open", side_effect=exc):
                start = time.time()
                outcome = self._try(wait=5)
            self.assertIn("Couldn't open the IdP refresh lock file", outcome)
            self.assertIn(exc.strerror, outcome)
            self.assertLess(time.time() - start, 2)

    def test_lock_error_other_than_held_is_reported(self):
        import errno
        with patch("fcntl.flock" if os.name != "nt" else "msvcrt.locking", side_effect=OSError(errno.EBADF, "Bad fd")):
            self.assertIn("Couldn't lock", self._try(wait=5))

    def test_lock_on_a_replaced_or_deleted_lock_file_is_taken_again(self):
        """Locked a file that another process meanwhile removed or replaced: let go and lock the one at the path."""
        other = tempfile.NamedTemporaryFile(delete=False)
        other.close()
        real_stat = os.stat
        calls = []

        def stat_once(path, *a, **kw):
            calls.append(path)
            if len(calls) == 1:
                return real_stat(other.name)          # replaced: the path is another file now
            if len(calls) == 2:
                raise FileNotFoundError(path)         # deleted
            return real_stat(path, *a, **kw)
        try:
            with patch.object(idp.os, "stat", side_effect=stat_once):
                self.assertEqual(self._try(), "locked")
            self.assertEqual(len(calls), 3)
        finally:
            os.remove(other.name)

    def test_slow_holder_is_never_taken_over(self):
        """Review finding #10: a holder older than the stale limit was taken over, and its clean-up then deleted
        the new holder's lock. An OS lock has no stale limit: an old lock file that is held stays held."""
        with idp.slot_lock(self.slot):
            os.utime(self._lock_path(), (0, 0))   # looks hours old
            self.assertIn("Timed out", self._try())
        self.assertEqual(self._try(), "locked")   # released when the holder is done

    def test_leftover_lock_file_is_not_a_held_lock(self):
        """A lock file left by a crashed process (or an older version) is taken at once: nothing holds it."""
        open(self._lock_path(), "w").close()
        self.assertEqual(self._try(wait=0), "locked")

    def test_lock_of_a_process_that_died_is_released(self):
        import subprocess
        code = ("import sys, time; from kinetic_devops import idp\n"
                "with idp.slot_lock(sys.argv[1]):\n"
                "    print('held', flush=True); time.sleep(60)\n")
        child = subprocess.Popen([sys.executable, "-c", code, self.slot], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            self.assertIn("Timed out", self._try())
            child.kill()
            child.wait(10)
            self.assertEqual(self._try(wait=5), "locked")
        finally:
            child.kill()
            child.stdout.close()

    def test_lock_serialises_refreshes(self):
        inside, overlap = [], []

        def worker():
            with idp.slot_lock(self.slot):
                if inside:
                    overlap.append(True)
                inside.append(1)
                time.sleep(0.1)
                inside.pop()
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(overlap, [])


@unittest.skipIf(os.name == "nt", "POSIX permissions; Windows uses the per-user temp directory")
class TestLockDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        os.chmod(self.tmp, 0o700)
        for root, dirs, _ in os.walk(self.tmp, topdown=False):
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(self.tmp)

    def test_private_directory_is_created_0700(self):
        path = idp.lock_dir()
        self.assertEqual(path, os.path.join(self.tmp, "kinetic-devops"))
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)

    def test_directory_others_can_access_is_refused(self):
        os.mkdir(os.path.join(self.tmp, "kinetic-devops"), 0o700)
        os.chmod(os.path.join(self.tmp, "kinetic-devops"), 0o777)
        with self.assertRaises(idp.IdPError):
            idp.lock_dir()

    def test_symlink_is_refused(self):
        os.mkdir(os.path.join(self.tmp, "elsewhere"), 0o700)
        os.symlink(os.path.join(self.tmp, "elsewhere"), os.path.join(self.tmp, "kinetic-devops"))
        try:
            with self.assertRaises(idp.IdPError):
                idp.lock_dir()
        finally:
            os.remove(os.path.join(self.tmp, "kinetic-devops"))


class TestAuthUsesIdpRefresh(unittest.TestCase):
    """get_active_config: an expired IdP session refreshes, or fails fast without a terminal; never asks for a password."""

    def _mgr(self, meta):
        mgr = KineticConfigManager.__new__(KineticConfigManager)
        servers = {"pilot": {"url": "https://pilot.example", "api_key": "key", "companies": "ACME",
                             "sessions": ["alice"]}}
        self.patches = [patch.object(mgr, "_get_server_dict", return_value=servers),
                        patch.object(mgr, "_get_token_key", return_value="slot"),
                        patch.object(mgr, "_get_token_meta", return_value=meta),
                        patch.object(mgr, "_fetch_token_kinetic", side_effect=AssertionError("password prompt")),
                        patch.object(sys.stdin, "isatty", return_value=False),
                        patch.object(sys.stdout, "isatty", return_value=False)]
        for p in self.patches:
            p.start()
        return mgr

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_expired_idp_session_is_refreshed(self):
        mgr = self._mgr({"auth_method": "idp", "_is_valid": False, "AccessToken": "OLD"})
        with patch.object(mgr, "_refresh_idp", return_value="NEW"):
            (token,) = mgr.get_active_config(("pilot", "alice", "ACME"), fields=("token",))
        self.assertEqual(token, "NEW")

    def test_unrefreshable_idp_session_fails_fast_without_terminal(self):
        mgr = self._mgr({"auth_method": "idp", "_is_valid": False, "AccessToken": "OLD"})
        with patch.object(mgr, "_refresh_idp", return_value=None), self.assertRaises(RuntimeError) as ctx:
            mgr.get_active_config(("pilot", "alice", "ACME"), fields=("token",))
        self.assertIn("idp-login pilot", str(ctx.exception))

    def test_unrefreshable_idp_session_signs_in_again_with_a_terminal_on_stdin(self):
        """stdin decides: stdout piped (`| tee`) with a person at the terminal opens the browser sign-in."""
        mgr = self._mgr({"auth_method": "idp", "_is_valid": False, "AccessToken": "OLD"})
        with patch.object(sys.stdin, "isatty", return_value=True), \
             patch.object(mgr, "_refresh_idp", return_value=None), \
             patch.object(mgr, "idp_login", return_value="BROWSER") as login:
            token = mgr._renew_idp_or_raise("slot", {}, "pilot", "alice", "ACME")
        self.assertEqual(token, "BROWSER")
        login.assert_called_once_with("pilot", "alice", "ACME")


class TestIdpLoginIdentity(unittest.TestCase):
    """idp_login stores the IdP identity and warns (doesn't refuse) when it doesn't look like --user."""

    def _login(self, username):
        mgr = KineticConfigManager.__new__(KineticConfigManager)
        cfg = {"url": "https://pilot.example", "api_key": "key", "companies": "ACME", "sessions": []}
        tok = {"access_token": "AT", "refresh_token": "RT", "idp_identity": {"sub": "s-1", "username": username}}
        stored = {}
        with patch.object(mgr, "_get_server_dict", return_value={"pilot": cfg}), \
             patch.object(mgr, "_find_env", return_value=("pilot", cfg)), \
             patch.object(idp, "discover", return_value=SETTINGS), \
             patch.object(idp, "get_client_id", return_value="cid"), \
             patch.object(idp, "login_browser", return_value=tok), \
             patch.object(mgr, "_validate_bearer_access", return_value=(True, "ok")), \
             patch.object(mgr, "_get_token_key", return_value="slot"), \
             patch.object(mgr, "touch_session", side_effect=lambda slot, meta, name: stored.update(meta)), \
             patch("builtins.print") as out:
            token = mgr.idp_login("pilot", "jdoe", "ACME")
        return token, stored, " ".join(str(c.args[0]) for c in out.call_args_list)

    def test_matching_identity_no_warning(self):
        token, stored, printed = self._login("jdoe@corp.example")
        self.assertEqual((token, stored["idp_username"]), ("AT", "jdoe@corp.example"))
        self.assertNotIn("⚠️", printed)

    def test_other_identity_warns_but_signs_in(self):
        token, stored, printed = self._login("bsmith@corp.example")
        self.assertEqual(token, "AT")
        self.assertIn("bsmith@corp.example", printed)
        self.assertIn("--user jdoe", printed)


class TestIdpLoginPrompts(unittest.TestCase):
    """idp_login asks for what isn't passed when a person is at the terminal; without one it stops before the browser."""

    def setUp(self):
        self.mgr = KineticConfigManager.__new__(KineticConfigManager)
        self.cfg = {"url": "https://pilot.example", "api_key": "key", "companies": "ACME,BETA", "sessions": []}
        self.stored = {}
        tok = {"access_token": "AT", "refresh_token": "RT",
               "idp_identity": {"sub": "s-1", "username": "JDoe@corp.example"}}
        self.login = MagicMock(return_value=tok)
        self.patches = [
            patch.object(self.mgr, "_get_server_dict", return_value={"pilot": self.cfg}),
            patch.object(self.mgr, "_find_env", side_effect=lambda servers, n: ("pilot", self.cfg) if n == "pilot" else (None, None)),
            patch.object(idp, "discover", return_value=SETTINGS),
            patch.object(idp, "get_client_id", return_value="cid"),
            patch.object(idp, "login_browser", self.login),
            patch.object(self.mgr, "_validate_bearer_access", return_value=(True, "ok")),
            patch.object(self.mgr, "_get_token_key", return_value="slot"),
            patch.object(self.mgr, "set_current_company"),
            patch.object(self.mgr, "touch_session", side_effect=lambda slot, meta, name: self.stored.update(meta)),
            patch("builtins.print"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def run_login(self, *args, tty=True, answers=(), **kw):
        with patch.object(sys.stdin, "isatty", return_value=tty), \
             patch("builtins.input", side_effect=list(answers)) as ask, \
             patch.object(self.mgr, "prompt_for_env", return_value=("pilot", "", "")) as pick:
            token = self.mgr.idp_login(*args, **kw)
        return token, ask, pick

    def test_everything_passed_asks_nothing(self):
        token, ask, pick = self.run_login("pilot", "jdoe", "BETA")
        self.assertEqual((token, self.stored["user_id"], self.stored["current_company"]), ("AT", "jdoe", "BETA"))
        ask.assert_not_called()
        pick.assert_not_called()

    def test_no_env_without_terminal_stops_before_the_browser(self):
        with self.assertRaises(SessionResolutionError) as ctx:
            self.run_login(tty=False, user_id="jdoe")
        self.assertIn("ENV", str(ctx.exception))
        self.login.assert_not_called()

    def test_no_user_without_terminal_stops_before_the_browser(self):
        with patch.dict(os.environ, {"KIN_USER": ""}), self.assertRaises(SessionResolutionError) as ctx:
            self.run_login("pilot", tty=False)
        self.assertIn("--user", str(ctx.exception))
        self.login.assert_not_called()

    def test_kin_user_still_counts_without_terminal(self):
        with patch.dict(os.environ, {"KIN_USER": "JDoe"}):
            token, ask, _ = self.run_login("pilot", tty=False)
        self.assertEqual((self.stored["user_id"], self.stored["current_company"]), ("jdoe", "ACME"))  # first company
        ask.assert_not_called()

    def test_no_env_with_terminal_picks_from_the_list_without_quick_connect(self):
        token, _, pick = self.run_login(user_id="jdoe", company_id="ACME")
        pick.assert_called_once_with(passive=True)
        self.assertEqual(token, "AT")

    def test_no_user_offers_the_idp_name_and_asks_the_company(self):
        with patch.dict(os.environ, {"KIN_USER": ""}):
            token, ask, _ = self.run_login("pilot", answers=["", "2"])   # Enter = take the suggestion; company 2
        prompts = [c.args[0] for c in ask.call_args_list]
        self.assertIn("[jdoe]", prompts[0])
        self.assertEqual((self.stored["user_id"], self.stored["current_company"]), ("jdoe", "BETA"))
        self.login.assert_called_once()   # the user was asked after the sign-in

    def test_typed_user_overrides_the_suggestion(self):
        with patch.dict(os.environ, {"KIN_USER": ""}):
            self.run_login("pilot", company_id="ACME", answers=["BSmith"])
        self.assertEqual(self.stored["user_id"], "bsmith")

    def test_user_given_company_asked_only_with_a_terminal(self):
        self.run_login("pilot", "jdoe", answers=["BETA"])
        self.assertEqual(self.stored["current_company"], "BETA")
        _, ask, _ = self.run_login("pilot", "jdoe", tty=False)
        self.assertEqual(self.stored["current_company"], "ACME")
        ask.assert_not_called()

    # --- without --new-session nothing changes ---

    def test_without_new_session_existing_sessions_and_kin_user_are_reused(self):
        self.cfg["sessions"] = ["alice", "bob"]
        with patch.dict(os.environ, {"KIN_USER": ""}):
            token, ask, _ = self.run_login("pilot", answers=["2", "1"])   # session list: bob; company ACME
        self.assertEqual((self.stored["user_id"], self.stored["current_company"]), ("bob", "ACME"))
        self.assertIn("Select Session", ask.call_args_list[0].args[0])
        self.assertIs(self.login.call_args.kwargs.get("force_login"), False)
        with patch.dict(os.environ, {"KIN_USER": "Carol"}):
            _, ask, _ = self.run_login("pilot", company_id="ACME")
        self.assertEqual(self.stored["user_id"], "carol")
        ask.assert_not_called()

    # --- --new-session ---

    def test_new_session_offers_no_sessions_ignores_kin_user_and_forces_the_idp_sign_in(self):
        self.cfg["sessions"] = ["alice", "bob"]
        with patch.dict(os.environ, {"KIN_USER": "Carol"}):
            token, ask, _ = self.run_login("pilot", answers=["", "2"], new_session=True)
        prompts = [c.args[0] for c in ask.call_args_list]
        self.assertTrue(prompts[0].startswith("Enter Epicor User ID [jdoe]"), prompts)   # no session list first
        self.assertFalse([p for p in prompts if "Session" in p])
        self.assertEqual((token, self.stored["user_id"], self.stored["current_company"]), ("AT", "jdoe", "BETA"))
        self.assertIs(self.login.call_args.kwargs["force_login"], True)

    def test_new_session_still_uses_values_passed(self):
        with patch.dict(os.environ, {"KIN_USER": "Carol"}):
            token, ask, pick = self.run_login("pilot", "BSmith", "BETA", new_session=True)
        self.assertEqual((self.stored["user_id"], self.stored["current_company"]), ("bsmith", "BETA"))
        ask.assert_not_called()
        pick.assert_not_called()

    def test_new_session_asks_for_the_environment_when_not_given(self):
        token, _, pick = self.run_login(user_id="jdoe", company_id="ACME", new_session=True)
        pick.assert_called_once_with(passive=True)
        self.assertEqual(token, "AT")

    def test_new_session_without_terminal_stops_before_the_browser(self):
        with self.assertRaises(SessionResolutionError) as ctx:
            self.run_login("pilot", "jdoe", "ACME", tty=False, new_session=True)
        self.assertIn("--new-session", str(ctx.exception))
        self.assertIn("pass ENV, --user and --co", str(ctx.exception))
        self.login.assert_not_called()

    def test_cli_flag_reaches_idp_login(self):
        from kinetic_devops import auth as auth_mod
        for argv, expected in ((["idp-login", "pilot", "--new-session"], True), (["idp-login", "pilot"], False)):
            with patch.object(sys, "argv", ["auth"] + argv), \
                 patch.object(auth_mod.KineticConfigManager, "__init__", return_value=None), \
                 patch.object(auth_mod.KineticConfigManager, "idp_login") as login:
                auth_mod.main()
            self.assertIs(login.call_args.kwargs["new_session"], expected)


class TestSelectSessionForEnv(unittest.TestCase):
    """The shared session/company picker (password sign-in uses it too) after splitting out the company choice."""

    def pick(self, answers, cfg=None, **kw):
        mgr = KineticConfigManager.__new__(KineticConfigManager)
        cfg = cfg or {"sessions": [], "companies": "ACME,BETA"}
        with patch("builtins.input", side_effect=list(answers)), patch("builtins.print"), \
             patch.object(mgr, "set_current_company") as setco:
            got = mgr._select_session_for_env("pilot", cfg, **kw)
        return got, setco

    def test_suggestion_is_the_default_for_a_new_user(self):
        got, setco = self.pick(["", "BETA"], suggested_user="jdoe")
        self.assertEqual(got, ("jdoe", "BETA"))
        setco.assert_called_once_with("pilot", "jdoe", "BETA")

    def test_without_suggestion_an_empty_user_id_is_asked_again(self):
        got, _ = self.pick(["", "alice", "1"])
        self.assertEqual(got, ("alice", "ACME"))

    def test_existing_session_and_single_company_unchanged(self):
        got, _ = self.pick(["2"], cfg={"sessions": ["alice", "bob"], "companies": "ACME"})
        self.assertEqual(got, ("bob", "ACME"))

    def test_offer_sessions_false_always_asks_for_a_user_id(self):
        got, _ = self.pick(["carol"], cfg={"sessions": ["alice", "bob"], "companies": "ACME"}, offer_sessions=False)
        self.assertEqual(got, ("carol", "ACME"))

    def test_preferred_company_skips_the_company_prompt(self):
        got, _ = self.pick(["1"], cfg={"sessions": ["alice"], "companies": "ACME,BETA"}, preferred_company="BETA")
        self.assertEqual(got, ("alice", "BETA"))


class FakeKeyring:
    """Dict-backed keyring that enforces Windows Credential Manager's ~1280-character limit."""

    def __init__(self):
        self.store = {}

    def set_password(self, service, user, value):
        if len(value) > 1280:
            raise OSError("CredWrite: The stub received bad data")
        self.store[(service, user)] = value

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user))


class TestSlotStorage(unittest.TestCase):
    def setUp(self):
        self.kr = FakeKeyring()
        self.patch = patch.object(idp, "keyring", self.kr)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_large_idp_session_round_trips_under_the_size_limit(self):
        meta = {"auth_method": "idp", "AccessToken": "a" * 3000, "refresh_token": "r" * 1500, "user_id": "alice",
                "_is_valid": True}
        stub = idp.pack("slot", meta)
        self.assertNotIn("AccessToken", stub)
        self.assertNotIn("_is_valid", stub)
        self.kr.set_password("slot", "current_token", "x" * 10)  # the stub itself is small
        self.assertTrue(all(len(v) <= 1280 for v in self.kr.store.values()))
        back = idp.unpack("slot", dict(stub))
        self.assertEqual((back["AccessToken"], back["refresh_token"]), (meta["AccessToken"], meta["refresh_token"]))

    def test_shorter_token_drops_stale_chunks(self):
        _store("slot", {"auth_method": "idp", "AccessToken": "a" * 3000, "refresh_token": "r"})
        _store("slot", {"auth_method": "idp", "AccessToken": "b" * 10, "refresh_token": "r"})
        stub = _stub("slot")
        self.assertEqual(idp.unpack("slot", dict(stub))["AccessToken"], "b" * 10)
        self.assertEqual(sorted(n for (_, n) in self.kr.store if n.startswith("idp_AccessToken#")),
                         idp._chunk_names("idp_AccessToken", stub["_idp_gens"]["AccessToken"]))

    def test_delete_and_move_secrets(self):
        _store("old", {"auth_method": "idp", "AccessToken": "a" * 1500, "refresh_token": "r"})
        current = idp.move_secrets("old", "new")
        self.kr.set_password("new", "current_token", current)
        self.assertTrue(idp.delete_session("old"))
        self.assertFalse([k for k in self.kr.store if k[0] == "old"])
        self.assertEqual(idp.unpack("new", _stub("new"))["AccessToken"], "a" * 1500)
        self.assertTrue(idp.delete_session("new"))
        self.assertEqual(self.kr.store, {})

    def test_password_sessions_are_untouched(self):
        meta = {"AccessToken": "jwt", "user_id": "alice"}
        self.assertIs(idp.pack("slot", meta), meta)
        self.assertEqual(self.kr.store, {})


def _fake_encrypt(obj, passphrase):
    """Stand-in for crypto.encrypt_json (no native library): an {"_enc": true} blob that hides the plaintext."""
    ct = base64.b64encode(json.dumps(obj).encode()).decode()[::-1]
    return json.dumps({"_enc": True, "ver": 1, "salt": "", "iv": hashlib.sha256(passphrase.encode()).hexdigest(),
                       "ct": ct})


def _fake_decrypt(blob, passphrase):
    wrapper = json.loads(blob)
    if wrapper.get("iv") != hashlib.sha256(passphrase.encode()).hexdigest():
        raise ValueError("InvalidTag")
    return json.loads(base64.b64decode(wrapper["ct"][::-1]))


def _store(slot, meta, passphrase=None):
    """Write a session the way auth does (idp.store_session), in clear or with the stand-in encryption."""
    idp.store_session(slot, meta, passphrase, lambda stub: idp.write_token_entry(
        slot, _fake_encrypt(stub, passphrase) if passphrase else json.dumps(stub)))


def _stub(slot, passphrase=None):
    raw = idp.read_token_entry(slot)
    return _fake_decrypt(raw, passphrase) if crypto.is_encrypted_blob(raw) else json.loads(raw)


def _side_raw(slot, field, passphrase=None):
    """The stored (possibly encrypted) value of an IdP session's token side entry."""
    return idp._read_chunks(slot, idp._side(field), _stub(slot, passphrase)["_idp_gens"][field])


def _set_side(slot, field, value, passphrase=None):
    """Replace one side entry's stored value (e.g. with one encrypted with another passphrase)."""
    stub = _stub(slot, passphrase)
    stub["_idp_gens"][field] = idp._write_chunks(slot, idp._side(field), idp._split(value), secrets.token_hex(4))
    idp.write_token_entry(slot, _fake_encrypt(stub, passphrase) if passphrase else json.dumps(stub))


class VaultFixture(unittest.TestCase):
    """FakeKeyring, stand-in encryption, KINETIC_VAULT_PASSPHRASE=pass-1, and one IdP session (self.meta)."""

    SECRETS = ("AT-SECRET", "RT-SECRET", "API-KEY-SECRET")

    def setUp(self):
        self.kr = FakeKeyring()
        self.servers = {"pilot": {"url": "https://pilot.example", "api_key": "API-KEY-SECRET", "companies": "ACME",
                                  "sessions": ["alice"]}}
        self.mgr = KineticConfigManager.__new__(KineticConfigManager)
        self.slot = self.mgr._get_token_key("pilot", "alice", "API-KEY-SECRET")
        self.meta = {"auth_method": "idp", "AccessToken": "AT-SECRET" + "a" * 2000, "refresh_token": "RT-SECRET",
                     "expires_in": 3600, "_local_timestamp": time.time(), "user_id": "alice", "env_name": "pilot",
                     "idp_tenant": "42", "idp_token_endpoint": SETTINGS["token"], "current_company": "ACME"}
        self.patches = [patch.object(idp, "keyring", self.kr), patch.object(auth, "keyring", self.kr),
                        patch.object(crypto, "encrypt_json", _fake_encrypt),
                        patch.object(crypto, "decrypt_json", _fake_decrypt),
                        patch.object(sys.stdout, "isatty", return_value=False),
                        patch.dict(os.environ, {"KINETIC_VAULT_PASSPHRASE": "pass-1"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def leaked(self):
        return sorted(n for (_, n), v in self.kr.store.items() if any(x in v for x in self.SECRETS))

    def encrypted_vault(self):
        self.kr.set_password(auth.SERVICE_SERVERS, "config", _fake_encrypt(self.servers, "pass-1"))


class TestEncryptedVaultIdpTokens(VaultFixture):
    """Review finding #8: with the vault encrypted, IdP tokens were written in clear (the idp_*#N side entries), and
    touch_session rewrote "current_token" (and the servers map) as plain JSON, undoing encrypt-vault."""

    def test_pack_with_the_vault_passphrase_stores_no_token_in_clear(self):
        stub = idp.pack("slot", {"auth_method": "idp", "AccessToken": "AT-SECRET", "refresh_token": "RT-SECRET"},
                        "pass-1")
        self.assertEqual(self.leaked(), [])
        back = idp.unpack("slot", dict(stub), "pass-1")
        self.assertEqual((back["AccessToken"], back["refresh_token"]), ("AT-SECRET", "RT-SECRET"))
        # without (or with a wrong) passphrase the tokens read as missing: sign in again, no traceback
        self.assertEqual(idp.unpack("slot", dict(stub))["refresh_token"], "")
        self.assertEqual(idp.unpack("slot", dict(stub), "wrong")["refresh_token"], "")

    def test_sign_in_and_refresh_keep_an_encrypted_vault_encrypted(self):
        self.encrypted_vault()
        self.servers["pilot"]["sessions"] = []   # touch_session registers the user: the servers map is rewritten
        self.kr.set_password(auth.SERVICE_SERVERS, "config", _fake_encrypt(self.servers, "pass-1"))
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        self.assertEqual(self.leaked(), [])
        self.assertTrue(crypto.is_encrypted_blob(self.kr.get_password(self.slot, "current_token")))
        servers = _fake_decrypt(self.kr.get_password(auth.SERVICE_SERVERS, "config"), "pass-1")
        self.assertEqual(servers["pilot"]["sessions"], ["alice"])
        back = self.mgr._get_token_meta(self.slot)
        self.assertEqual((back["AccessToken"], back["refresh_token"]), (self.meta["AccessToken"], "RT-SECRET"))
        self.assertTrue(back["_is_valid"])
        self.mgr.set_current_company("pilot", "alice", "BETA")   # the other write path
        self.assertEqual(self.leaked(), [])
        self.assertEqual(self.mgr._get_token_meta(self.slot)["current_company"], "BETA")

    def test_unencrypted_vault_is_unchanged(self):
        self.kr.set_password(auth.SERVICE_SERVERS, "config", json.dumps(self.servers))
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        stub = json.loads(self.kr.get_password(self.slot, "current_token"))
        self.assertTrue(stub["_idp_split"])
        self.assertEqual(_side_raw(self.slot, "refresh_token"), "RT-SECRET")
        self.assertEqual(self.mgr._get_token_meta(self.slot)["refresh_token"], "RT-SECRET")

    def test_no_passphrase_refuses_to_write_in_clear(self):
        self.encrypted_vault()
        os.environ.pop("KINETIC_VAULT_PASSPHRASE")
        with self.assertRaises(RuntimeError) as ctx:
            self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        self.assertIn("KINETIC_VAULT_PASSPHRASE", str(ctx.exception))
        self.assertEqual(self.leaked(), [])

    def test_wrong_passphrase_refuses_to_write(self):
        self.encrypted_vault()
        os.environ["KINETIC_VAULT_PASSPHRASE"] = "typo"
        with self.assertRaises(RuntimeError) as ctx:
            self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        self.assertIn("doesn't open the vault", str(ctx.exception))
        self.assertIsNone(self.kr.get_password(self.slot, "current_token"))

    def test_encrypt_vault_encrypts_idp_side_entries_and_decrypt_vault_restores_them(self):
        self.kr.set_password(auth.SERVICE_SERVERS, "config", json.dumps(self.servers))
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")   # plain vault: tokens in clear
        self.assertTrue(self.leaked())
        with patch("builtins.print"):
            self.mgr.encrypt_vault("pass-1")
        self.assertEqual(self.leaked(), [])
        self.assertEqual(self.mgr._get_token_meta(self.slot)["refresh_token"], "RT-SECRET")
        with patch("builtins.print"):
            self.mgr.decrypt_vault("pass-1")
        self.assertFalse(crypto.is_encrypted_blob(self.kr.get_password(self.slot, "current_token")))
        self.assertEqual(_side_raw(self.slot, "refresh_token"), "RT-SECRET")

    def test_encrypt_vault_repairs_a_slot_left_half_clear_by_older_versions(self):
        """Older encrypt-vault encrypted the stub but left the side entries in clear; running it again fixes that."""
        self.encrypted_vault()
        stub = idp.pack(self.slot, dict(self.meta))   # side entries in clear
        self.kr.set_password(self.slot, "current_token", _fake_encrypt(stub, "pass-1"))
        self.assertTrue([n for n in self.leaked() if n.startswith("idp_refresh_token#")])
        with patch("builtins.print"):
            self.mgr.encrypt_vault("pass-1")
        self.assertEqual(self.leaked(), [])
        self.assertEqual(self.mgr._get_token_meta(self.slot)["refresh_token"], "RT-SECRET")

    def test_encrypt_vault_leaves_a_token_it_cant_decrypt_as_it_is(self):
        """A side entry encrypted with another passphrase was read as "" and written back as an empty token, and
        not counted as a failure. Now the slot is left untouched and counted."""
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        _set_side(self.slot, "refresh_token", _fake_encrypt("RT-OTHER", "other-pass"), "pass-1")
        before = dict(self.kr.store)
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.mgr.encrypt_vault("pass-1")
        self.assertIn("1 session(s) couldn't be read", out.getvalue())
        self.assertEqual({k: v for k, v in self.kr.store.items() if k[0] == self.slot},
                         {k: v for k, v in before.items() if k[0] == self.slot})

    def test_encrypt_vault_counts_a_missing_chunk_and_leaves_the_slot(self):
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        marker = _stub(self.slot, "pass-1")["_idp_gens"]["AccessToken"]
        self.kr.delete_password(self.slot, idp._chunk_names("idp_AccessToken", marker)[1])
        before = {k: v for k, v in self.kr.store.items() if k[0] == self.slot}
        with patch("builtins.print") as printed:
            self.mgr.encrypt_vault("pass-1")
        self.assertIn("1 session(s)", " ".join(str(c.args[0]) for c in printed.call_args_list))
        self.assertEqual({k: v for k, v in self.kr.store.items() if k[0] == self.slot}, before)

    def test_session_whose_token_cant_be_decrypted_is_not_valid(self):
        """_get_token_meta returned _is_valid True with an empty AccessToken when a side entry didn't decrypt."""
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        _set_side(self.slot, "AccessToken", _fake_encrypt("AT-OTHER", "other-pass"), "pass-1")
        meta = self.mgr._get_token_meta(self.slot)
        self.assertEqual(meta["AccessToken"], "")
        self.assertFalse(meta["_is_valid"])
        self.assertEqual(meta["_remaining"], 0)


class TestIdpRefreshAsksForThePassphraseFirst(VaultFixture):
    """Review finding: with --vault-prompt every, a refresh asked for the passphrase only to store the new tokens,
    after the refresh token was spent and while the lock was held; a typo lost the rotated refresh token and was a
    traceback instead of the sign-in-again path."""

    def setUp(self):
        super().setUp()
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.meta, _local_timestamp=time.time() - 7200), "pilot")
        os.environ.pop("KINETIC_VAULT_PASSPHRASE")
        self.mgr._vault_pass = None   # a new run
        self.mgr.vault_prompt = "every"
        self.req = MagicMock()
        self.req.post.return_value = _resp({"access_token": "AT-NEW", "refresh_token": "RT-NEW", "expires_in": 3600})
        for p in (patch.object(sys.stdout, "isatty", return_value=True), patch.object(idp, "requests", self.req),
                  patch.object(idp, "get_client_id", return_value="cid")):
            p.start()
            self.patches.append(p)

    def stored(self):
        with patch.dict(os.environ, {"KINETIC_VAULT_PASSPHRASE": "pass-1"}):
            return self.mgr._get_token_meta(self.slot)

    def refresh(self, *typed):
        err = io.StringIO()
        with patch("kinetic_devops.auth.getpass.getpass", side_effect=list(typed)) as ask, \
             patch.object(sys, "stderr", err):
            token = self.mgr._refresh_idp(self.slot, dict(self.meta, _is_valid=False), "pilot")
        return token, ask.call_count, err.getvalue()

    def test_right_passphrase_is_asked_once_before_the_refresh(self):
        token, asked, _ = self.refresh("pass-1")
        self.assertEqual(token, "AT-NEW")
        self.assertEqual(asked, 1)   # not again to re-read, to store the tokens or to register the session
        self.req.post.assert_called_once()
        self.assertEqual((self.stored()["AccessToken"], self.stored()["refresh_token"]), ("AT-NEW", "RT-NEW"))
        self.assertEqual(self.leaked(), [])
        self.assertIsNone(self.mgr._pinned_pass())   # nothing kept after the refresh

    def test_typo_spends_nothing_and_takes_the_sign_in_again_path(self):
        token, asked, err = self.refresh("typo")
        self.assertIsNone(token)
        self.assertEqual(asked, 1)
        self.req.post.assert_not_called()   # the refresh token was not spent
        self.assertIn("doesn't open the vault", err)
        self.assertEqual(self.stored()["refresh_token"], "RT-SECRET")
        # without a terminal on stdin: the friendly "run idp-login" error, not the vault error's traceback
        with patch("kinetic_devops.auth.getpass.getpass", return_value="typo"), patch.object(sys, "stderr", io.StringIO()), \
             patch.object(sys.stdin, "isatty", return_value=False), self.assertRaises(RuntimeError) as ctx:
            self.mgr._renew_idp_or_raise(self.slot, dict(self.meta, _is_valid=False), "pilot", "alice", "ACME")
        self.assertNotIsInstance(ctx.exception, auth.VaultError)
        self.assertIn("idp-login pilot", str(ctx.exception))
        self.req.post.assert_not_called()

    def test_no_passphrase_at_all_spends_nothing(self):
        token, _, err = self.refresh(EOFError())
        self.assertIsNone(token)
        self.req.post.assert_not_called()
        self.assertIn("KINETIC_VAULT_PASSPHRASE", err)


class TestVaultPassphraseIsCheckedAgainstTheServersMap(VaultFixture):
    """Review finding: a passphrase that opened a token slot was kept, and a write then took it without checking,
    so it could re-encrypt the servers map with a passphrase nobody else uses."""

    def test_passphrase_that_opens_only_a_slot_is_not_kept(self):
        self.encrypted_vault()
        os.environ.pop("KINETIC_VAULT_PASSPHRASE")
        self.kr.set_password(self.slot, "current_token",
                             _fake_encrypt({"AccessToken": "AT-SECRET", "user_id": "alice"}, "slot-only"))
        servers_before = self.kr.get_password(auth.SERVICE_SERVERS, "config")
        with patch.object(sys.stdout, "isatty", return_value=True), patch.object(sys, "stderr", io.StringIO()), \
             patch("kinetic_devops.auth.getpass.getpass", return_value="slot-only"):
            self.assertEqual(self.mgr._get_token_meta(self.slot)["AccessToken"], "AT-SECRET")
            self.assertIsNone(getattr(self.mgr, "_vault_pass", None))
            with self.assertRaisesRegex(auth.VaultError, "doesn't open the vault"):
                self.mgr._save_server_dict(dict(self.servers))
        self.assertEqual(self.kr.get_password(auth.SERVICE_SERVERS, "config"), servers_before)

    def test_vault_passphrase_that_opens_a_slot_is_kept(self):
        self.encrypted_vault()
        os.environ.pop("KINETIC_VAULT_PASSPHRASE")
        self.kr.set_password(self.slot, "current_token",
                             _fake_encrypt({"AccessToken": "AT-SECRET", "user_id": "alice"}, "pass-1"))
        with patch.object(sys.stdout, "isatty", return_value=True), patch.object(sys, "stderr", io.StringIO()), \
             patch("kinetic_devops.auth.getpass.getpass", return_value="pass-1") as ask:
            self.mgr._get_token_meta(self.slot)
            self.mgr._save_server_dict(dict(self.servers))
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(self.mgr._vault_pass, "pass-1")


def _jwt(length):
    """A JWT-shaped token of about this many characters (Kinetic's access tokens are 600-1100)."""
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    head, sig = enc({"alg": "RS256", "typ": "JWT"}), "S" * 342
    claims = {"iss": "https://kinetic.example/Env", "sub": "alice", "aud": "epicor_erp", "exp": 1, "pad": ""}
    while len(f"{head}.{enc(claims)}.{sig}") < length:
        claims["pad"] += "p"
    return f"{head}.{enc(claims)}.{sig}"


class TestPasswordSessionChunking(VaultFixture):
    """Review finding: only IdP sessions were split; a password session's whole token response went into one
    "current_token" entry, which encryption makes ~1.4x longer, over Windows Credential Manager's ~1280 characters
    for Kinetic tokens of about 660-1050 characters. FakeKeyring enforces the limit."""

    SECRETS = ("API-KEY-SECRET",)

    def setUp(self):
        super().setUp()
        self.jwt = _jwt(1000)
        self.SECRETS = ("API-KEY-SECRET", self.jwt[200:260])   # any piece of the token in clear is a leak
        self.pw = {"AccessToken": self.jwt, "ExpiresIn": 1200, "TokenType": "Bearer", "_local_timestamp": time.time(),
                   "user_id": "alice", "env_name": "pilot", "current_company": "ACME"}

    def entries(self, slot=None):
        return {n: v for (s, n), v in self.kr.store.items() if s == (slot or self.slot)}

    def check_round_trip(self, slot=None):
        self.assertTrue(all(len(v) <= 1280 for v in self.kr.store.values()))
        back = self.mgr._get_token_meta(slot or self.slot)
        self.assertEqual(back["AccessToken"], self.jwt)
        self.assertTrue(back["_is_valid"])

    def test_encrypted_password_session_is_chunked_and_reads_back(self):
        self.assertGreater(len(_fake_encrypt(self.pw, "pass-1")), 1280)   # would not fit in one entry
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.pw), "pilot")
        self.assertTrue(self.kr.get_password(self.slot, "current_token").startswith("chunks:"))
        self.assertEqual(self.leaked(), [])
        self.check_round_trip()

    def test_clear_password_session_that_fits_stays_one_entry(self):
        self.assertLess(len(json.dumps(self.pw)), 1280)
        self.kr.set_password(auth.SERVICE_SERVERS, "config", json.dumps(self.servers))
        self.mgr.touch_session(self.slot, dict(self.pw), "pilot")
        self.assertEqual(list(self.entries()), ["current_token"])
        self.assertEqual(json.loads(self.kr.get_password(self.slot, "current_token"))["AccessToken"], self.jwt)
        self.check_round_trip()

    def test_clear_password_session_over_the_limit_is_chunked(self):
        self.jwt = self.pw["AccessToken"] = _jwt(1500)
        self.kr.set_password(auth.SERVICE_SERVERS, "config", json.dumps(self.servers))
        self.mgr.touch_session(self.slot, dict(self.pw), "pilot")
        self.assertTrue(self.kr.get_password(self.slot, "current_token").startswith("chunks:"))
        self.check_round_trip()

    def test_old_single_entry_form_still_reads(self):
        self.encrypted_vault()
        small = dict(self.pw, AccessToken="jwt")
        self.kr.set_password(self.slot, "current_token", _fake_encrypt(small, "pass-1"))
        self.assertEqual(self.mgr._get_token_meta(self.slot)["AccessToken"], "jwt")

    def test_going_back_to_one_entry_removes_the_chunks(self):
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.pw), "pilot")
        self.mgr.touch_session(self.slot, dict(self.pw, AccessToken="short"), "pilot")
        self.assertEqual(list(self.entries()), ["current_token"])
        self.assertEqual(self.mgr._get_token_meta(self.slot)["AccessToken"], "short")

    def test_encrypt_and_decrypt_vault_handle_chunked_password_sessions(self):
        self.kr.set_password(auth.SERVICE_SERVERS, "config", json.dumps(self.servers))
        self.mgr.touch_session(self.slot, dict(self.pw), "pilot")   # clear: one entry
        with patch("builtins.print"):
            self.mgr.encrypt_vault("pass-1")
        self.assertTrue(self.kr.get_password(self.slot, "current_token").startswith("chunks:"))
        self.assertEqual(self.leaked(), [])
        self.check_round_trip()
        with patch("builtins.print"):
            self.mgr.decrypt_vault("pass-1")
        self.assertEqual(list(self.entries()), ["current_token"])
        self.check_round_trip()

    def test_rename_moves_the_chunks_and_removes_the_old_ones(self):
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.pw), "pilot")
        self.mgr.rename("pilot", "pilot2")
        new_slot = self.mgr._get_token_key("pilot2", "alice", "API-KEY-SECRET")
        self.assertEqual(self.entries(), {})
        self.assertTrue(self.kr.get_password(new_slot, "current_token").startswith("chunks:"))
        self.check_round_trip(new_slot)

    def test_logout_removes_the_chunks(self):
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.pw), "pilot")
        with patch("builtins.print"):
            self.mgr.logout("pilot")
        self.assertEqual(self.entries(), {})

    def test_validate_reads_chunked_and_idp_sessions_and_keeps_them(self):
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.pw), "pilot")
        with patch("builtins.print") as printed:
            self.mgr.validate()
        report = " ".join(str(a) for c in printed.call_args_list for a in c.args)
        self.assertNotIn("removed", report)
        self.assertNotIn("missing AccessToken", report)


class InterruptingKeyring(FakeKeyring):
    """FakeKeyring that runs hook(service, user) after every write, or raises on the write it names."""

    def __init__(self):
        super().__init__()
        self.hook = None
        self.fail_on = None

    def set_password(self, service, user, value):
        if self.fail_on and self.fail_on(service, user):
            raise OSError("killed")
        super().set_password(service, user, value)
        if self.hook:
            self.hook(service, user)


class TestChunkedWritesAreNeverTorn(unittest.TestCase):
    """Review finding: a chunked value that stayed chunked was overwritten in place (chunks first, then the count),
    so a reader between two chunk writes got a mix (unreadable), and a writer stopped after the first chunk left the
    slot unreadable for good. Each chunked write is now a new generation, switched to by one write."""

    OLD, NEW = "o" * 2500, "n" * 2600   # both over the limit: chunked, three chunks each

    def setUp(self):
        self.kr = InterruptingKeyring()
        self.patch = patch.object(idp, "keyring", self.kr)
        self.patch.start()
        idp.write_token_entry("slot", self.OLD)

    def tearDown(self):
        self.patch.stop()

    def test_a_reader_between_any_two_writes_sees_the_old_or_the_new_value(self):
        seen = []
        self.kr.hook = lambda service, user: seen.append(idp.read_token_entry("slot"))
        idp.write_token_entry("slot", self.NEW)
        self.assertTrue(seen)
        self.assertEqual(set(seen) - {self.OLD, self.NEW}, set())
        self.assertEqual(idp.read_token_entry("slot"), self.NEW)

    def test_a_reader_during_the_old_chunks_clean_up_sees_the_new_value(self):
        seen = []
        real_delete = self.kr.delete_password

        def delete(service, user):
            real_delete(service, user)
            seen.append(idp.read_token_entry("slot"))
        with patch.object(self.kr, "delete_password", side_effect=delete):
            idp.write_token_entry("slot", self.NEW)
        self.assertEqual(set(seen), {self.NEW})

    def test_a_write_stopped_after_its_first_chunk_leaves_the_old_value(self):
        written = []

        def fail(service, user):
            written.append(user)
            return len(written) == 2   # the first new chunk went in; the second write fails
        self.kr.fail_on = fail
        with self.assertRaises(OSError):
            idp.write_token_entry("slot", self.NEW)
        self.assertEqual(idp.read_token_entry("slot"), self.OLD)

    def test_going_to_one_entry_is_one_write_and_drops_the_chunks(self):
        seen = []
        self.kr.hook = lambda service, user: seen.append(idp.read_token_entry("slot"))
        idp.write_token_entry("slot", '{"AccessToken": "short"}')
        self.assertEqual(seen, ['{"AccessToken": "short"}'])
        self.assertEqual(list(self.kr.store), [("slot", "current_token")])

    def test_a_reader_of_an_idp_session_between_any_two_writes_sees_the_old_or_the_new_one(self):
        """Review finding: the IdP tokens and the stub were switched one after another, so a crash or a reader
        between them got the new access token with the rotated-away refresh token. The stub is the commit point."""
        old = {"auth_method": "idp", "AccessToken": self.OLD, "refresh_token": "RA" * 600}
        new = {"auth_method": "idp", "AccessToken": self.NEW, "refresh_token": "RB" * 600}
        _store("s2", old)
        seen = []

        def read(service, user):
            back = idp.unpack("s2", _stub("s2"))
            seen.append((back["AccessToken"], back["refresh_token"]))
        self.kr.hook = read
        with patch.object(self.kr, "delete_password", side_effect=lambda s, u: (FakeKeyring.delete_password(
                self.kr, s, u), read(s, u))):
            _store("s2", new)
        self.assertEqual(set(seen) - {(self.OLD, "RA" * 600), (self.NEW, "RB" * 600)}, set())
        self.assertEqual(seen[-1], (self.NEW, "RB" * 600))

    def test_a_new_generation_never_reuses_the_current_one(self):
        with patch.object(idp.secrets, "token_hex", side_effect=["deadbeef", "deadbeef", "cafebabe"]):
            idp.write_token_entry("s3", "A" * 1500)
            idp.write_token_entry("s3", "B" * 1500)
        self.assertEqual(idp.read_token_entry("s3"), "B" * 1500)
        self.assertTrue(self.kr.get_password("s3", "current_token").startswith("chunks:cafebabe:"))

    def test_marker_parsing_is_strict(self):
        for bad in ("chunks:abc:99999", "chunks:a-b:2", "chunks:ab:", "\u00b2", "chunks::2", "65"):
            self.assertIsNone(idp._chunk_names("current_token", bad), bad)
        self.assertEqual(idp._chunk_names("x", "chunks:ab12:2"), ["x#ab12.0", "x#ab12.1"])

    def test_marker_is_neither_json_nor_digits(self):
        marker = self.kr.get_password("slot", "current_token")
        self.assertRegex(marker, r"^chunks:[0-9a-f]+:3$")
        with self.assertRaises(ValueError):
            json.loads(marker)   # older versions read the entry as missing, instead of failing on an int

    def test_earlier_forms_still_read_and_are_cleaned_up(self):
        kr = FakeKeyring()
        with patch.object(idp, "keyring", kr):
            # a bare count with numbered chunks (IdP side entries before generations; this branch's first chunking)
            kr.set_password("s", "current_token", "2")
            kr.set_password("s", "current_token#0", '{"AccessToken": ')
            kr.set_password("s", "current_token#1", '"legacy"}')
            kr.set_password("s", "idp_AccessToken", "1")
            kr.set_password("s", "idp_AccessToken#0", "AT-LEGACY")
            self.assertEqual(idp.read_token_entry("s"), '{"AccessToken": "legacy"}')
            self.assertEqual(idp.unpack("s", {"_idp_split": True})["AccessToken"], "AT-LEGACY")
            idp.write_token_entry("s", "x" * 1500)
            self.assertNotIn(("s", "current_token#0"), kr.store)
            self.assertEqual(idp.read_token_entry("s"), "x" * 1500)
            _store("s", {"auth_method": "idp", "AccessToken": "AT-NEW", "refresh_token": "r"})
            self.assertNotIn(("s", "idp_AccessToken"), kr.store)
            self.assertNotIn(("s", "idp_AccessToken#0"), kr.store)
            self.assertEqual(idp.unpack("s", _stub("s"))["AccessToken"], "AT-NEW")
            self.assertTrue(idp.delete_session("s"))
            self.assertEqual(kr.store, {})

    def test_main_format_idp_session_reads_and_is_replaced(self):
        """Stored by main before generations: a stub without "_idp_gens" and per-field count entries."""
        kr = FakeKeyring()
        with patch.object(idp, "keyring", kr):
            for field, value in (("AccessToken", "A" * 1500), ("refresh_token", "R" * 900)):
                parts = [value[i:i + 1000] for i in range(0, len(value), 1000)]
                for i, part in enumerate(parts):
                    kr.set_password("s", f"idp_{field}#{i}", part)
                kr.set_password("s", f"idp_{field}", str(len(parts)))
            kr.set_password("s", "current_token", json.dumps({"_idp_split": True, "user_id": "alice"}))
            back = idp.unpack("s", _stub("s"))
            self.assertEqual((back["AccessToken"], back["refresh_token"]), ("A" * 1500, "R" * 900))
            _store("s", dict(back, auth_method="idp", AccessToken="A2"))
            self.assertEqual(idp.unpack("s", _stub("s"))["refresh_token"], "R" * 900)
            self.assertFalse([n for (_, n) in kr.store if n in ("idp_AccessToken", "idp_refresh_token")
                              or n.startswith(("idp_AccessToken#", "idp_refresh_token#")) and "." not in n])


class TestUnreadableTokensAreNeverSaved(VaultFixture):
    """Review finding: an IdP session whose side entries didn't decrypt read as AccessToken/refresh_token "", and
    set_current_company / touch_from_headers saved that, encrypting the empty strings over the real tokens."""

    def half_encrypted_slot(self):
        """A clear stub with encrypted side entries, as an interrupted encrypt-vault can leave it."""
        self.encrypted_vault()
        stub = idp.pack(self.slot, dict(self.meta), "pass-1")
        idp.write_token_entry(self.slot, json.dumps(stub))
        os.environ.pop("KINETIC_VAULT_PASSPHRASE")
        self.mgr._vault_pass = None
        self.mgr.vault_prompt = "every"

    def tokens(self):
        with patch.dict(os.environ, {"KINETIC_VAULT_PASSPHRASE": "pass-1"}):
            meta = self.mgr._get_token_meta(self.slot)
        return meta["AccessToken"], meta["refresh_token"]

    def test_the_passphrase_is_asked_for_encrypted_side_entries_under_a_clear_stub(self):
        self.half_encrypted_slot()
        with patch.object(sys.stdout, "isatty", return_value=True), \
             patch("kinetic_devops.auth.getpass.getpass", return_value="pass-1") as ask:
            meta = self.mgr._get_token_meta(self.slot)
        ask.assert_called_once()
        self.assertEqual(meta["refresh_token"], "RT-SECRET")
        self.assertNotIn("_tokens_unreadable", meta)

    def test_set_current_company_doesnt_save_tokens_it_couldnt_read(self):
        """Side entries encrypted with a passphrase other than the vault's: they don't read, nothing is saved."""
        self.encrypted_vault()
        idp.write_token_entry(self.slot, json.dumps(idp.pack(self.slot, dict(self.meta), "other-pass")))
        before = {k: v for k, v in self.kr.store.items() if k[0] == self.slot}
        with patch("builtins.print") as printed:
            self.mgr.set_current_company("pilot", "alice", "BETA")
        self.assertIn("couldn't be decrypted", str(printed.call_args))
        self.assertEqual({k: v for k, v in self.kr.store.items() if k[0] == self.slot}, before)

    def test_save_refuses_a_meta_whose_tokens_didnt_read(self):
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        with self.assertRaisesRegex(auth.VaultError, "couldn't be decrypted"):
            self.mgr._save_token_meta(self.slot, dict(self.meta, AccessToken="", _tokens_unreadable=True))
        self.assertEqual(self.tokens(), (self.meta["AccessToken"], "RT-SECRET"))

    def test_touch_from_headers_skips_a_session_whose_tokens_didnt_read(self):
        unreadable = dict(self.meta, refresh_token="", _tokens_unreadable=True)
        with patch.object(self.mgr, "get_session_by_bearer", return_value=("pilot", "alice")), \
             patch.object(self.mgr, "_get_api_key", return_value="API-KEY-SECRET"), \
             patch.object(self.mgr, "_get_token_meta", return_value=unreadable), \
             patch.object(self.mgr, "touch_session") as touch:
            self.mgr.touch_from_headers({"Authorization": "Bearer AT", "X-Epicor-Company": "BETA"})
        touch.assert_not_called()

    def test_rejected_refresh_token_is_dropped_even_when_the_access_token_didnt_read(self):
        """Review finding: the invalid_grant branch kept "_tokens_unreadable", so storing the dropped refresh token
        was refused with a misleading message and the dead token was sent again on every run."""
        stored = []
        meta = {"AccessToken": "", "refresh_token": "RT1", "idp_tenant": "42", "idp_token_endpoint": SETTINGS["token"],
                "_is_valid": False, "_tokens_unreadable": True, "_tokens_locked": True}
        req = MagicMock()
        req.post.return_value = _resp({"error": "invalid_grant"}, 400)
        with patch.object(idp, "requests", req), patch.object(idp, "get_client_id", return_value="cid"):
            idp.refresh_session(meta, reread=lambda: dict(meta), store=stored.append, slot=f"s-{time.time()}")
        self.assertEqual(stored[-1]["refresh_token"], "")
        self.assertFalse(set(stored[-1]) & set(idp.UNREAD_FLAGS))

    def test_a_missing_chunk_on_a_clear_vault_asks_for_no_passphrase(self):
        """Review finding: a missing chunk set the same flag as an encrypted token, so a clear vault asked for a
        vault passphrase."""
        self.kr.set_password(auth.SERVICE_SERVERS, "config", json.dumps(self.servers))
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        os.environ.pop("KINETIC_VAULT_PASSPHRASE")
        marker = _stub(self.slot)["_idp_gens"]["AccessToken"]
        self.kr.delete_password(self.slot, idp._chunk_names("idp_AccessToken", marker)[0])
        with patch.object(sys.stdout, "isatty", return_value=True), \
             patch("kinetic_devops.auth.getpass.getpass", return_value="") as ask:
            meta = self.mgr._get_token_meta(self.slot)
        ask.assert_not_called()
        self.assertTrue(meta["_tokens_unreadable"])
        self.assertFalse(meta["_is_valid"])

    def test_refresh_saves_new_tokens_over_an_unreadable_access_token(self):
        stored = []
        meta = {"AccessToken": "", "refresh_token": "RT1", "idp_tenant": "42", "idp_token_endpoint": SETTINGS["token"],
                "_is_valid": False, "_tokens_unreadable": True}
        req = MagicMock()
        req.post.return_value = _resp({"access_token": "NEW", "refresh_token": "RT2", "expires_in": 3600})
        with patch.object(idp, "requests", req), patch.object(idp, "get_client_id", return_value="cid"):
            idp.refresh_session(meta, reread=lambda: dict(meta), store=stored.append, slot=f"s-{time.time()}")
        self.assertNotIn("_tokens_unreadable", stored[-1])


class Crash(BaseException):
    """A process killed at this point (BaseException: no except clause in the code under test catches it)."""


class CrashingKeyring(FakeKeyring):
    """FakeKeyring that dies after `budget` more writes or deletes."""

    budget = None

    def _tick(self):
        if self.budget is not None:
            if self.budget == 0:
                raise Crash()
            self.budget -= 1

    def set_password(self, service, user, value):
        if len(value) > 1280:
            raise OSError("CredWrite: The stub received bad data")
        self._tick()
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        self._tick()
        self.store.pop((service, user))


class TestCrashAtEveryWrite(unittest.TestCase):
    """Kill the process at every keyring write or delete of an operation: what's read afterwards is the session
    from before or the one from after, whole (never a new access token with the old, rotated-away refresh token),
    and a logged-out session never comes back. Based on the review's crash harness."""

    SERVERS = {"pilot": {"url": "https://pilot.example", "api_key": "K", "companies": "ACME", "sessions": ["alice"]}}

    def setUp(self):
        self.kr = CrashingKeyring()
        self.patches = [patch.object(idp, "keyring", self.kr), patch.object(auth, "keyring", self.kr),
                        patch.object(crypto, "encrypt_json", _fake_encrypt),
                        patch.object(crypto, "decrypt_json", _fake_decrypt),
                        patch.object(sys.stdout, "isatty", return_value=False),
                        patch.dict(os.environ, {"KINETIC_VAULT_PASSPHRASE": "pass-1"})]
        for p in self.patches:
            p.start()
        self.slot = self.mgr()._get_token_key("pilot", "alice", "K")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    @staticmethod
    def mgr():
        return KineticConfigManager.__new__(KineticConfigManager)

    @staticmethod
    def idp_meta(at, rt, co="ACME"):
        return {"auth_method": "idp", "AccessToken": at, "refresh_token": rt, "expires_in": 3600,
                "_local_timestamp": time.time(), "user_id": "alice", "env_name": "pilot", "idp_tenant": "t",
                "idp_token_endpoint": SETTINGS["token"], "current_company": co}

    @staticmethod
    def pw_meta(at, co="ACME"):
        return {"AccessToken": at, "expires_in": 3600, "_local_timestamp": time.time(), "user_id": "alice",
                "env_name": "pilot", "current_company": co}

    @staticmethod
    def sig(meta):
        return None if meta is None else (meta.get("AccessToken"), meta.get("refresh_token"),
                                          meta.get("current_company"))

    def readback(self, slot=None):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return self.sig(self.mgr()._get_token_meta(slot or self.slot))

    def prepare(self, encrypted, meta):
        self.kr.budget = None
        self.kr.store = {}
        self.kr.store[(auth.SERVICE_SERVERS, "config")] = (_fake_encrypt(self.SERVERS, "pass-1") if encrypted
                                                            else json.dumps(self.SERVERS))
        self.mgr().touch_session(self.slot, copy.deepcopy(meta), "pilot")
        return copy.deepcopy(self.kr.store)

    def crash_everywhere(self, base, action, check):
        """Run action from the stored state `base`, killed after 0, 1, 2, ... writes until it completes; check()
        after each run. Returns the number of writes the action makes."""
        k = 0
        while True:
            self.kr.store = copy.deepcopy(base)
            self.kr.budget = k
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    action()
                done = True
            except Crash:
                done = False
            self.kr.budget = None
            check(k, done)
            if done:
                return k
            k += 1

    def test_touch_session(self):
        big_a, big_b = "A" * 1500, "B" * 1500
        cases = [("pw big->big", self.pw_meta(big_a), self.pw_meta(big_b)),
                 ("pw small->big", self.pw_meta("A" * 50), self.pw_meta(big_b)),
                 ("pw big->small", self.pw_meta(big_a), self.pw_meta("B" * 50)),
                 ("idp->idp", self.idp_meta("A" * 2500, "RA" * 600), self.idp_meta("B" * 2500, "RB" * 600)),
                 ("pw->idp", self.pw_meta(big_a), self.idp_meta("B" * 2500, "RB" * 600)),
                 ("idp->pw", self.idp_meta("A" * 2500, "RA" * 600), self.pw_meta(big_b))]
        for encrypted in (False, True):
            for label, old, new in cases:
                with self.subTest(label, encrypted=encrypted):
                    base = self.prepare(encrypted, old)
                    allowed = {self.sig(old), self.sig(new)}

                    def check(k, done):
                        self.assertIn(self.readback(), allowed, f"killed after write {k}")
                        if done:
                            self.assertEqual(self.readback(), self.sig(new))
                    self.crash_everywhere(base, lambda: self.mgr().touch_session(self.slot, copy.deepcopy(new),
                                                                                   "pilot"), check)

    def entries(self):
        return sorted(u for (s, u) in self.kr.store if s == self.slot)

    def used_entries(self):
        """The entries the stored session uses: "current_token", the index, and the chunks they name."""
        used = {"current_token"}
        listed = idp._read_gens(self.slot)
        if listed:
            used.add(idp.GENS_ENTRY)
        for entry, marker in listed:
            used.update(idp._chunk_names(entry, marker))
        return sorted(used)

    def test_nothing_is_left_behind_after_a_crash(self):
        """Review finding: chunks of a crashed chunked "current_token" write were in no index, so nothing (logout
        included) ever deleted them, and on a clear vault they hold the token response in clear. Every generation
        is now listed before its chunks are written: after a crash, logout removes everything, and the next write
        removes what the crash left."""
        cases = [("idp->idp", self.idp_meta("A" * 2500, "RA" * 600), self.idp_meta("B" * 2500, "RB" * 600)),
                 ("pw->idp", self.pw_meta("A" * 1500), self.idp_meta("B" * 2500, "RB" * 600)),
                 ("idp->pw", self.idp_meta("A" * 2500, "RA" * 600), self.pw_meta("B" * 1500)),
                 ("pw big->big", self.pw_meta("A" * 1500), self.pw_meta("B" * 1500))]
        for encrypted in (False, True):
            for label, old, new in cases:
                with self.subTest(label, encrypted=encrypted):
                    base = self.prepare(encrypted, old)

                    def check(k, done):
                        crashed = copy.deepcopy(self.kr.store)
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.mgr().logout("pilot")
                        self.assertEqual(self.entries(), [], f"left after a crash at write {k} and logout")
                        self.kr.store = copy.deepcopy(crashed)
                        self.mgr().touch_session(self.slot, copy.deepcopy(new), "pilot")
                        self.assertEqual(self.entries(), self.used_entries(), f"crash at write {k}, then a write")
                        self.assertEqual(self.readback(), self.sig(new))
                    self.crash_everywhere(base, lambda: self.mgr().touch_session(self.slot, copy.deepcopy(new),
                                                                                   "pilot"), check)

    def test_set_current_company(self):
        meta = self.idp_meta("A" * 2500, "RA" * 600)
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                base = self.prepare(encrypted, meta)
                allowed = {self.sig(meta), self.sig(dict(meta, current_company="BETA"))}
                self.crash_everywhere(base, lambda: self.mgr().set_current_company("pilot", "alice", "BETA"),
                                      lambda k, done: self.assertIn(self.readback(), allowed, f"write {k}"))

    def test_logout(self):
        for encrypted in (False, True):
            for label, meta in (("pw big", self.pw_meta("A" * 1500)), ("idp", self.idp_meta("A" * 2500, "R" * 1200))):
                with self.subTest(label, encrypted=encrypted):
                    base = self.prepare(encrypted, meta)

                    def check(k, done):
                        got = self.readback()
                        if k > 0 or done:   # the first write is the delete of "current_token": gone at once
                            self.assertIsNone(got, f"session back after a crash at write {k}")
                        else:
                            self.assertIn(got, (self.sig(meta), None))
                        if done:
                            self.assertEqual([u for (s, u) in self.kr.store if s == self.slot], [])
                    self.crash_everywhere(base, lambda: self.mgr().logout("pilot"), check)

    def test_encrypt_and_decrypt_vault(self):
        for label, meta in (("pw big", self.pw_meta("A" * 1500)), ("idp", self.idp_meta("A" * 2500, "RA" * 600)),
                            ("pw small", self.pw_meta("A" * 30))):
            for encrypted, action in ((False, lambda: self.mgr().encrypt_vault("pass-1")),
                                      (True, lambda: self.mgr().decrypt_vault("pass-1"))):
                with self.subTest(label, encrypted=encrypted):
                    base = self.prepare(encrypted, meta)
                    self.crash_everywhere(base, action, lambda k, done: self.assertEqual(
                        self.readback(), self.sig(meta), f"write {k}"))

    def test_rename(self):
        new_slot = self.mgr()._get_token_key("pilot2", "alice", "K")
        for encrypted in (False, True):
            for label, meta in (("pw big", self.pw_meta("A" * 1500)), ("idp", self.idp_meta("A" * 2500, "R" * 1200))):
                with self.subTest(label, encrypted=encrypted):
                    base = self.prepare(encrypted, meta)

                    def check(k, done):
                        renamed = "pilot2" in self.mgr()._get_server_dict()
                        self.assertEqual(self.readback(new_slot if renamed else self.slot), self.sig(meta),
                                         f"write {k}")
                        if done:
                            self.assertEqual([u for (s, u) in self.kr.store if s == self.slot], [])
                    self.crash_everywhere(base, lambda: self.mgr().rename("pilot", "pilot2"), check)


class TestReadersRacingAWriter(VaultFixture):
    """Review finding: a reader that read the marker (or the stub) just before another process switched to a new
    generation and deleted the old one found a chunk missing and read the session as gone (password prompt, or a
    headless failure). A missing chunk under a marker or stub that changed meanwhile is read again."""

    def race(self, entry_prefix, write):
        """Run write() once, just before the reader's first read of an entry starting with entry_prefix."""
        real_get = FakeKeyring.get_password
        fired = []

        def get(service, user):
            if service == self.slot and user.startswith(entry_prefix) and not fired:
                fired.append(1)
                write()
            return real_get(self.kr, service, user)
        return patch.object(self.kr, "get_password", side_effect=get), fired

    def test_chunked_password_session(self):
        self.encrypted_vault()
        pw = {"AccessToken": "A" * 1500, "expires_in": 3600, "_local_timestamp": time.time(), "user_id": "alice"}
        self.mgr.touch_session(self.slot, dict(pw), "pilot")
        racing, fired = self.race("current_token#", lambda: KineticConfigManager.__new__(
            KineticConfigManager).touch_session(self.slot, dict(pw, AccessToken="B" * 1500), "pilot"))
        with racing:
            meta = self.mgr._get_token_meta(self.slot)
        self.assertTrue(fired)
        self.assertEqual(meta["AccessToken"], "B" * 1500)
        self.assertTrue(meta["_is_valid"])

    def test_idp_session_on_a_clear_vault_asks_for_nothing(self):
        self.kr.set_password(auth.SERVICE_SERVERS, "config", json.dumps(self.servers))
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        os.environ.pop("KINETIC_VAULT_PASSPHRASE")
        newer = dict(self.meta, AccessToken="AT-NEW" + "b" * 2000, refresh_token="RT-NEW")
        racing, fired = self.race("idp_AccessToken#", lambda: KineticConfigManager.__new__(
            KineticConfigManager).touch_session(self.slot, dict(newer), "pilot"))
        with racing, patch.object(sys.stdout, "isatty", return_value=True), \
             patch("kinetic_devops.auth.getpass.getpass", return_value="") as ask:
            meta = self.mgr._get_token_meta(self.slot)
        self.assertTrue(fired)
        ask.assert_not_called()
        self.assertEqual((meta["AccessToken"], meta["refresh_token"]), (newer["AccessToken"], "RT-NEW"))
        self.assertNotIn("_tokens_unreadable", meta)


class TestWritersNeverInterleave(VaultFixture):
    """Review finding: only a refresh took the slot lock. A company update (touch_from_headers runs after every API
    call) that ran while a refresh stored its rotated tokens indexed and then deleted the refresh's uncommitted
    chunks, or lost its index update: the refreshed session read as ('', '') after its refresh token was spent, and
    logout or rename then missed chunks. Every write, logout and rename now holds the slot's lock, and a
    read-modify-write re-reads the session under it. Writers run in separate threads here (the lock is reentrant
    within a thread, as a refresh storing its result needs)."""

    def setUp(self):
        super().setUp()
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.meta, AccessToken="A0" * 1250, refresh_token="R0" * 650), "pilot")

    def other(self):
        return KineticConfigManager.__new__(KineticConfigManager)

    def race(self, when, second):
        """Run a store of rotated tokens (A1/R1) in one thread; when its keyring write matches when(user), start
        second() in another thread and give it time to run. Returns (second was still waiting, its thread)."""
        real_set = FakeKeyring.set_password
        state = {}

        def set_password(service, user, value):
            if service == self.slot and when(user) and "thread" not in state:
                state["thread"] = threading.Thread(target=second, daemon=True)
                state["thread"].start()
                state["thread"].join(0.5)
                state["blocked"] = state["thread"].is_alive()
            return real_set(self.kr, service, user, value)

        def first():
            with patch.object(self.kr, "set_password", side_effect=set_password):
                self.other().touch_session(self.slot, dict(self.meta, AccessToken="A1" * 1250,
                                                            refresh_token="R1" * 650), "pilot")
        t = threading.Thread(target=first)
        t.start()
        t.join(30)
        state["thread"].join(30)
        return state["blocked"]

    def check_session(self, company):
        meta = self.mgr._get_token_meta(self.slot)
        self.assertEqual((meta["AccessToken"], meta["refresh_token"], meta["current_company"]),
                         ("A1" * 1250, "R1" * 650, company))
        self.assertFalse(set(meta) & set(idp.UNREAD_FLAGS))
        used = {"current_token", idp.GENS_ENTRY}
        for entry, marker in idp._read_gens(self.slot):
            used.update(idp._chunk_names(entry, marker))
        self.assertEqual({u for (s, u) in self.kr.store if s == self.slot}, used)   # nothing unlisted, nothing missing

    def test_company_update_waits_for_a_store_in_progress(self):
        for label, when in (("at the commit point", lambda u: u == "current_token"),
                            ("after the index write", lambda u: "#" in u)):
            with self.subTest(label):
                self.setUp()
                blocked = self.race(when, lambda: self.other().set_current_company("pilot", "alice", "BETA"))
                self.assertTrue(blocked, "the company update didn't wait for the store's lock")
                self.check_session("BETA")   # re-read under the lock: the rotated tokens are kept
                self.tearDown()

    def test_touch_from_headers_waits_and_re_reads(self):
        def touch():
            other = self.other()
            with patch.object(other, "get_session_by_bearer", return_value=("pilot", "alice")):
                other.touch_from_headers({"Authorization": "Bearer A0", "X-Epicor-Company": "BETA"})
        self.assertTrue(self.race(lambda u: u == "current_token", touch))
        self.check_session("BETA")

    def test_logout_after_racing_writers_leaves_nothing(self):
        self.race(lambda u: "#" in u, lambda: self.other().set_current_company("pilot", "alice", "BETA"))
        with patch("builtins.print"):
            self.mgr.logout("pilot")
        self.assertEqual([u for (s, u) in self.kr.store if s == self.slot], [])

    def test_hot_path_skips_the_write_when_the_lock_is_busy(self):
        """touch_from_headers runs after every API call: when another process holds the lock (a refresh), it waits a
        moment, then skips recording the company instead of failing or holding up the call."""
        held, release = threading.Event(), threading.Event()

        def holder():
            with idp.slot_lock(self.slot):
                held.set()
                release.wait(10)
        t = threading.Thread(target=holder)
        t.start()
        held.wait(5)
        before = dict(self.kr.store)
        try:
            with patch.object(auth, "HOT_PATH_LOCK_WAIT_SEC", 0.3), \
                 patch.object(self.mgr, "get_session_by_bearer", return_value=("pilot", "alice")):
                start = time.time()
                self.mgr.touch_from_headers({"Authorization": "Bearer A0", "X-Epicor-Company": "BETA"})
                self.assertLess(time.time() - start, 3)
        finally:
            release.set()
            t.join(10)
        self.assertEqual(self.kr.store, before)

    def test_set_current_company_says_so_on_a_lock_timeout(self):
        with patch.object(idp, "slot_lock", side_effect=idp.LockTimeout("Timed out waiting.")), \
             patch("builtins.print") as printed:
            self.mgr.set_current_company("pilot", "alice", "BETA")
        self.assertIn("wasn't saved", str(printed.call_args))

    def test_the_lock_is_reentrant_in_a_thread_and_exclusive_across_threads(self):
        outcome = []
        with idp.slot_lock(self.slot):
            with idp.slot_lock(self.slot, wait=0):
                outcome.append("reentered")

            def other_thread():
                try:
                    with idp.slot_lock(self.slot, wait=0.3):
                        outcome.append("taken")
                except idp.LockTimeout:
                    outcome.append("waited")
            t = threading.Thread(target=other_thread)
            t.start()
            t.join(5)
        self.assertEqual(outcome, ["reentered", "waited"])
        with idp.slot_lock(self.slot, wait=0):   # released by the outer block
            pass


class TestDamagedIndex(VaultFixture):
    """Review finding: an index of null or 5 made every write fail with TypeError (after a refresh had rotated the
    token), and logout's bare except hid it and left the chunks. A damaged index is ignored, and what the stored
    session uses is still found (the chunked "current_token" marker, a clear stub's "_idp_gens")."""

    def test_reads_writes_and_logout_survive_a_damaged_index(self):
        bads = ("null", "5", '{"a":1}', "not json", '[["idp_AccessToken"]]',
                json.dumps([["x", "chunks:ab:1"]] * 200), json.dumps([["idp_AccessToken", "chunks:ab:999"]]))
        for encrypted, bad in [(e, b) for e in (False, True) for b in bads]:
            with self.subTest(bad=bad[:20], encrypted=encrypted):
                self.kr.store = {}
                self.kr.set_password(auth.SERVICE_SERVERS, "config", _fake_encrypt(self.servers, "pass-1")
                                     if encrypted else json.dumps(self.servers))
                self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
                self.kr.store[(self.slot, idp.GENS_ENTRY)] = bad
                self.assertEqual(self.mgr._get_token_meta(self.slot)["refresh_token"], "RT-SECRET")
                self.mgr.set_current_company("pilot", "alice", "BETA")
                meta = self.mgr._get_token_meta(self.slot)
                self.assertEqual((meta["refresh_token"], meta["current_company"]), ("RT-SECRET", "BETA"))
                self.assertIsInstance(json.loads(self.kr.get_password(self.slot, idp.GENS_ENTRY)), list)
                self.kr.store[(self.slot, idp.GENS_ENTRY)] = bad
                with patch("builtins.print"):
                    self.mgr.logout("pilot")
                self.assertEqual([u for (s, u) in self.kr.store if s == self.slot], [])


class LockHolder:
    """Holds a slot's lock in another thread (another process refreshing) until released."""

    def __init__(self, slot):
        self.slot, self.held, self.release = slot, threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        with idp.slot_lock(self.slot):
            self.held.set()
            self.release.wait(30)

    def __enter__(self):
        self.thread.start()
        self.held.wait(5)
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(10)


def short_lock_waits(seconds=0.3):
    """Every lock wait cut to `seconds` (a busy session times out quickly)."""
    real = idp.slot_lock
    return patch.object(idp, "slot_lock", lambda slot, wait=30.0: real(slot, wait=min(wait, seconds)))


class TestBusySessions(VaultFixture):
    """Review findings: what a session another process holds (a refresh) does to rename, logout, the vault commands
    and sign-in."""

    def setUp(self):
        super().setUp()
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.meta, AccessToken="A0" * 1250, refresh_token="R0" * 650), "pilot")

    def tokens(self, slot=None):
        meta = self.mgr._get_token_meta(slot or self.slot)
        return None if meta is None else (meta["AccessToken"][:2], meta["refresh_token"][:2])

    def test_rename_holds_the_old_slot_until_it_is_deleted(self):
        """A refresh under the old name between rename's copy and its delete spent R0 and stored R1 in the old slot;
        rename then deleted it, leaving the new slot with the spent R0. The refresh now waits for rename, finds the
        session gone and doesn't refresh; the new slot keeps the unspent R0."""
        req = MagicMock()
        req.post.return_value = _resp({"access_token": "A1" * 1250, "refresh_token": "R1" * 650, "expires_in": 3600})
        real_move, started = idp.move_secrets, {}

        def refresh_in_another_process():
            other = KineticConfigManager.__new__(KineticConfigManager)
            started["token"] = other._refresh_idp(self.slot, dict(self.meta, _is_valid=False), "pilot", force=True)

        def move(old, new):
            current = real_move(old, new)
            started["thread"] = threading.Thread(target=refresh_in_another_process)
            started["thread"].start()
            started["thread"].join(0.5)
            started["waited"] = started["thread"].is_alive()
            return current
        with patch.object(idp, "requests", req), patch.object(idp, "get_client_id", return_value="cid"), \
             patch.object(idp, "move_secrets", move), patch.object(sys, "stderr", io.StringIO()):
            self.mgr.rename("pilot", "pilot2")
            started["thread"].join(30)
        self.assertTrue(started["waited"])
        self.assertIsNone(started["token"])
        req.post.assert_not_called()   # R0 not spent
        self.assertEqual(self.tokens(self.mgr._get_token_key("pilot2", "alice", "API-KEY-SECRET")), ("A0", "R0"))
        self.assertEqual([u for (s, u) in self.kr.store if s == self.slot], [])

    def test_rename_of_a_busy_session_changes_nothing(self):
        before = dict(self.kr.store)
        with LockHolder(self.slot), short_lock_waits(), self.assertRaisesRegex(RuntimeError, "nothing was renamed"):
            self.mgr.rename("pilot", "pilot2")
        self.assertEqual(self.kr.store, before)

    def test_logout_of_a_busy_session_keeps_it_listed(self):
        """logout counted a lock timeout as done: the user left the sessions list and "purged" was printed while
        the slot still held the refresh token."""
        with LockHolder(self.slot), short_lock_waits(), patch("builtins.print") as printed:
            self.mgr.logout("pilot")
        out = " ".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn("not cleared", out)
        self.assertNotIn("purged", out)
        self.assertEqual(self.mgr._get_server_dict()["pilot"]["sessions"], ["alice"])
        self.assertEqual(self.tokens(), ("A0", "R0"))
        with patch("builtins.print"):
            self.mgr.logout("pilot")   # not busy any more
        self.assertIsNone(self.tokens())
        self.assertEqual(self.mgr._get_server_dict()["pilot"]["sessions"], [])

    def test_api_key_change_with_a_busy_session_keeps_the_key(self):
        with LockHolder(self.slot), short_lock_waits(), patch("builtins.print") as printed:
            self.mgr.set_api_key("pilot", "NEW-KEY")
        self.assertIn("was not changed", str(printed.call_args_list))
        self.assertEqual(self.mgr._get_server_dict()["pilot"]["api_key"], "API-KEY-SECRET")
        self.assertEqual(self.tokens(), ("A0", "R0"))

    def test_vault_commands_report_a_busy_session_and_rerunning_finishes(self):
        """A busy slot was counted as unreadable and left in clear in an encrypted vault (or encrypted in a clear
        one). It is retried at the end, waiting longer; if still busy, it is reported as busy, and a rerun does it."""
        with patch("builtins.print"):
            self.mgr.decrypt_vault("pass-1")
        clear = lambda: [u for (s, u), v in self.kr.store.items() if s == self.slot and ("R0R0" in v or "A0A0" in v)]
        self.assertTrue(clear())
        with LockHolder(self.slot), short_lock_waits(), patch("builtins.print") as printed:
            self.mgr.encrypt_vault("pass-1")
        out = " ".join(str(c.args[0]) for c in printed.call_args_list)
        self.assertIn("busy", out)
        self.assertIn("run encrypt-vault again", out)
        self.assertNotIn("couldn't be read", out)
        with patch("builtins.print"):
            self.mgr.encrypt_vault("pass-1")
        self.assertEqual(clear(), [])
        self.assertEqual(self.tokens(), ("A0", "R0"))

    def test_a_slot_busy_for_a_moment_is_resealed_by_the_retry(self):
        holder = LockHolder(self.slot)
        calls = []
        real = idp.slot_lock

        def lock(slot, wait=30.0):
            calls.append(wait)
            if len(calls) == 1:
                return real(slot, wait=0.2)   # busy on the first pass ...
            holder.release.set()               # ... free for the retry
            holder.thread.join(10)
            return real(slot, wait=wait)
        with patch("builtins.print"):
            self.mgr.decrypt_vault("pass-1")
        with holder, patch.object(idp, "slot_lock", lock), patch("builtins.print") as printed:
            self.mgr.encrypt_vault("pass-1")
        self.assertNotIn("busy", str(printed.call_args_list))
        self.assertEqual(calls[1], auth.SIGN_IN_LOCK_WAIT_SEC)

    def test_storing_a_sign_in_waits_longer_and_says_so(self):
        holder = LockHolder(self.slot)
        with holder, patch("builtins.print") as printed:
            threading.Timer(0.5, holder.release.set).start()
            self.mgr._store_sign_in(self.slot, dict(self.meta, AccessToken="A2" * 1250, refresh_token="R2" * 650),
                                    "pilot")
        self.assertIn("Waiting for another process", str(printed.call_args_list))
        self.assertEqual(self.tokens(), ("A2", "R2"))

    def test_refresh_uses_short_timeouts_and_a_busy_lock_takes_the_sign_in_again_path(self):
        req = MagicMock()
        req.post.return_value = _resp({"access_token": "A1", "refresh_token": "R1"})
        with patch.object(idp, "requests", req):
            idp.refresh(SETTINGS["token"], "cid", "R0")
        self.assertEqual(req.post.call_args.kwargs["timeout"], idp.REFRESH_TIMEOUT)
        self.assertLessEqual(sum(idp.REFRESH_TIMEOUT), 30)   # never longer than others wait for the lock
        with patch.object(idp, "refresh_session", side_effect=idp.LockTimeout("Timed out waiting.")), \
             patch.object(sys, "stderr", io.StringIO()) as err:
            self.assertIsNone(self.mgr._refresh_idp(self.slot, dict(self.meta), "pilot"))
        self.assertIn("wasn't refreshed", err.getvalue())

    def test_touch_from_headers_never_fails_the_api_call(self):
        with patch.object(self.mgr, "get_session_by_bearer", return_value=("pilot", "alice")), \
             patch.object(self.mgr, "_get_token_meta", side_effect=ValueError("anything")):
            self.mgr.touch_from_headers({"Authorization": "Bearer A0", "X-Epicor-Company": "BETA"})


class TestLockAndPassphraseDetails(VaultFixture):
    def test_reentering_twice_keeps_other_threads_out(self):
        """Mutation check: the inner exit must restore the depth, not forget the lock."""
        slot = f"reenter-{time.time()}"
        outcome = []

        def other():
            try:
                with idp.slot_lock(slot, wait=0.2):
                    outcome.append("taken")
            except idp.LockTimeout:
                outcome.append("waited")
        with idp.slot_lock(slot):
            with idp.slot_lock(slot, wait=0):
                pass
            with idp.slot_lock(slot, wait=0):
                t = threading.Thread(target=other)
                t.start()
                t.join(5)
            t = threading.Thread(target=other)
            t.start()
            t.join(5)
        self.assertEqual(outcome, ["waited", "waited"])

    def test_the_write_passphrase_is_asked_before_the_lock_is_taken(self):
        """set_current_company asks for the passphrase (--vault-prompt every) while the session's lock is still free,
        so nobody waits on a person typing."""
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        os.environ.pop("KINETIC_VAULT_PASSPHRASE")
        self.mgr._vault_pass = None
        self.mgr.vault_prompt = "every"
        free_when_asked = []

        def ask(prompt):
            def probe():
                try:
                    with idp.slot_lock(self.slot, wait=0):
                        free_when_asked.append(True)
                except idp.LockTimeout:
                    free_when_asked.append(False)
            t = threading.Thread(target=probe)
            t.start()
            t.join(5)
            return "pass-1"
        with patch.object(sys.stdout, "isatty", return_value=True), patch.object(sys, "stderr", io.StringIO()), \
             patch("kinetic_devops.auth.getpass.getpass", side_effect=ask):
            self.mgr.set_current_company("pilot", "alice", "BETA")
        self.assertTrue(free_when_asked)
        self.assertNotIn(False, free_when_asked, "asked for the passphrase while holding the lock")
        with patch.dict(os.environ, {"KINETIC_VAULT_PASSPHRASE": "pass-1"}):
            self.assertEqual(self.mgr._get_token_meta(self.slot)["current_company"], "BETA")

    def test_pinned_passphrase_is_per_thread(self):
        seen = []
        with self.mgr._vault_pass_pinned("pass-A"):
            t = threading.Thread(target=lambda: seen.append(self.mgr._pinned_pass()))
            t.start()
            t.join(5)
            self.assertEqual(self.mgr._pinned_pass(), "pass-A")
        self.assertEqual(seen, [None])
        self.assertIsNone(self.mgr._pinned_pass())

    @unittest.skipUnless(hasattr(os, "fork"), "fork")
    def test_a_forked_child_holds_none_of_its_parents_locks(self):
        slot = f"fork-{time.time()}"
        with idp.slot_lock(slot):
            pid = os.fork()
            if pid == 0:   # child: the parent holds the lock; the child must wait for it, not re-enter
                code = 1
                try:
                    with idp.slot_lock(slot, wait=0.2):
                        code = 2
                except idp.LockTimeout:
                    code = 0
                finally:
                    os._exit(code)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)


class TestVaultPromptMode(unittest.TestCase):
    """A typed vault passphrase is kept for the rest of the run (--vault-prompt once, the default, said once on stderr)
    or asked for each time (--vault-prompt every / KINETIC_VAULT_PROMPT=every)."""

    def setUp(self):
        self.kr = FakeKeyring()
        servers = {"pilot": {"url": "https://pilot.example", "api_key": "K", "companies": "ACME", "sessions": []}}
        self.kr.set_password(auth.SERVICE_SERVERS, "config", _fake_encrypt(servers, "pass-1"))
        self.mgr = KineticConfigManager.__new__(KineticConfigManager)
        env = {k: v for k, v in os.environ.items() if k not in ("KINETIC_VAULT_PASSPHRASE", "KINETIC_VAULT_PROMPT")}
        self.patches = [patch.object(idp, "keyring", self.kr), patch.object(auth, "keyring", self.kr),
                        patch.object(crypto, "encrypt_json", _fake_encrypt),
                        patch.object(crypto, "decrypt_json", _fake_decrypt),
                        patch.object(sys.stdout, "isatty", return_value=True),
                        patch.dict(os.environ, env, clear=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def read_twice(self):
        err = io.StringIO()
        with patch("kinetic_devops.auth.getpass.getpass", return_value="pass-1") as ask, \
             patch.object(sys, "stderr", err):
            self.mgr._get_server_dict()
            self.mgr._save_server_dict(self.mgr._get_server_dict())
        return ask.call_count, err.getvalue()

    def test_once_is_the_default_asks_once_and_says_so(self):
        asked, err = self.read_twice()
        self.assertEqual(asked, 1)
        self.assertEqual(err.count("kept in memory for the rest of this run"), 1)
        self.assertIn("--vault-prompt every", err)

    def test_every_asks_each_time_and_keeps_nothing(self):
        self.mgr.vault_prompt = "every"
        asked, err = self.read_twice()
        self.assertEqual(asked, 3)                      # two reads and the write's check
        self.assertIsNone(getattr(self.mgr, "_vault_pass", None))
        self.assertNotIn("kept in memory", err)

    def test_every_from_the_environment(self):
        with patch.dict(os.environ, {"KINETIC_VAULT_PROMPT": "EVERY"}):
            asked, _ = self.read_twice()
        self.assertEqual(asked, 3)

    def test_option_beats_the_environment(self):
        self.mgr.vault_prompt = "once"
        with patch.dict(os.environ, {"KINETIC_VAULT_PROMPT": "every"}):
            asked, _ = self.read_twice()
        self.assertEqual(asked, 1)

    def test_passphrase_from_the_environment_is_not_announced(self):
        with patch.dict(os.environ, {"KINETIC_VAULT_PASSPHRASE": "pass-1"}):
            asked, err = self.read_twice()
        self.assertEqual((asked, err), (0, ""))

    def test_bad_mode_is_an_error(self):
        with patch.dict(os.environ, {"KINETIC_VAULT_PROMPT": "sometimes"}), \
             patch("kinetic_devops.auth.getpass.getpass", return_value="pass-1"), \
             self.assertRaisesRegex(RuntimeError, "once or every"):
            self.mgr._get_server_dict()


@unittest.skipUnless(native_checks_enabled(), "native check deferred (loads cryptography's native library)")
class TestEncryptedVaultIdpTokensNative(unittest.TestCase):
    def test_real_encryption_round_trip_fits_the_keyring_limit(self):
        kr = FakeKeyring()
        meta = {"auth_method": "idp", "AccessToken": "AT-SECRET" + "a" * 3000, "refresh_token": "RT-SECRET",
                "idp_token_endpoint": SETTINGS["token"], "idp_tenant": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "user_id": "firstname.lastname", "env_name": "MyProductionEnvironment",
                "idp_username": "firstname.lastname@corp.example", "idp_subject": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"}
        try:
            with patch.object(idp, "keyring", kr):
                stub = idp.pack("slot", meta, "pass-1")
                kr.set_password("slot", "current_token", crypto.encrypt_json(stub, "pass-1"))  # FakeKeyring: <= 1280
                back = idp.unpack("slot", crypto.decrypt_json(kr.get_password("slot", "current_token"), "pass-1"),
                                  "pass-1")
        except RuntimeError as exc:   # cryptography not installed, or not loadable
            self.skipTest(f"vault encryption unavailable: {exc}")
        self.assertEqual((back["AccessToken"], back["refresh_token"]), (meta["AccessToken"], "RT-SECRET"))
        self.assertFalse([k for k, v in kr.store.items() if "RT-SECRET" in v or "AT-SECRET" in v])

    def test_encrypted_password_session_with_a_kinetic_sized_token_fits(self):
        """Real encryption: a 1000-character access token encrypts to ~1700 characters, over the limit: chunked."""
        kr = FakeKeyring()
        servers = {"pilot": {"url": "https://pilot.example", "api_key": "K", "companies": "ACME", "sessions": []}}
        mgr = KineticConfigManager.__new__(KineticConfigManager)
        meta = {"AccessToken": _jwt(1000), "ExpiresIn": 1200, "TokenType": "Bearer", "_local_timestamp": time.time(),
                "user_id": "alice", "env_name": "pilot", "current_company": "ACME"}
        try:
            with patch.object(idp, "keyring", kr), patch.object(auth, "keyring", kr), \
                 patch.dict(os.environ, {"KINETIC_VAULT_PASSPHRASE": "pass-1"}):
                kr.set_password(auth.SERVICE_SERVERS, "config", crypto.encrypt_json(servers, "pass-1"))
                mgr.touch_session("slot", dict(meta), "pilot")
                back = mgr._get_token_meta("slot")
        except auth.VaultError:
            raise
        except RuntimeError as exc:   # cryptography not installed, or not loadable
            self.skipTest(f"vault encryption unavailable: {exc}")
        self.assertTrue(kr.get_password("slot", "current_token").startswith("chunks:"))
        self.assertEqual(back["AccessToken"], meta["AccessToken"])
        self.assertFalse([k for k, v in kr.store.items() if meta["AccessToken"][100:160] in v])


class TestScannerLearnsIdpIdentifiers(unittest.TestCase):
    def test_tenant_endpoint_and_client_id_patterns(self):
        mgr = KineticConfigManager.__new__(KineticConfigManager)
        servers = {"pilot": {"url": "https://pilot.example", "api_key": "k" * 20, "companies": "ACME",
                             "sessions": ["alice"]}}
        cid = "0f0e0d0c-0b0a-4908-8706-050403020100"
        with patch.object(mgr, "_get_server_dict", return_value=servers),              patch.object(mgr, "_get_token_meta", return_value={"auth_method": "idp", "idp_tenant": "4242"}),              patch.object(idp, "get_client_id", return_value=cid):
            pats = mgr.get_sensitive_data_patterns()
        self.assertTrue(pats["IDP_TENANT_ENDPOINT"].search("https://idp.example/tid/4242/connect"))
        self.assertFalse(pats["IDP_TENANT_ENDPOINT"].search("total 4242 rows"))
        self.assertTrue(pats["IDP_CLIENT_IDS"].search(f"client {cid.upper()}"))


if __name__ == "__main__":
    unittest.main()
