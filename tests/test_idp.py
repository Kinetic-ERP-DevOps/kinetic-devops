"""Tests for kinetic_devops.idp (Epicor IdP sign-in) and its use in auth. No network: requests is mocked."""

import base64
import hashlib
import io
import json
import os
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
        idp.pack("slot", {"auth_method": "idp", "AccessToken": "a" * 3000, "refresh_token": "r"})
        idp.pack("slot", {"auth_method": "idp", "AccessToken": "b" * 10, "refresh_token": "r"})
        self.assertEqual(idp.unpack("slot", {"_idp_split": True})["AccessToken"], "b" * 10)
        self.assertNotIn(("slot", "idp_AccessToken#2"), self.kr.store)

    def test_delete_and_move_secrets(self):
        idp.pack("old", {"auth_method": "idp", "AccessToken": "a" * 1500, "refresh_token": "r"})
        idp.move_secrets("old", "new")
        idp.delete_secrets("old")
        self.assertFalse([k for k in self.kr.store if k[0] == "old"])
        self.assertEqual(idp.unpack("new", {"_idp_split": True})["AccessToken"], "a" * 1500)

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
        self.assertEqual(idp._get_chunked(self.slot, "idp_refresh_token"), "RT-SECRET")
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
        self.assertEqual(idp._get_chunked(self.slot, "idp_refresh_token"), "RT-SECRET")

    def test_encrypt_vault_repairs_a_slot_left_half_clear_by_older_versions(self):
        """Older encrypt-vault encrypted the stub but left the side entries in clear; running it again fixes that."""
        self.encrypted_vault()
        stub = idp.pack(self.slot, dict(self.meta))   # side entries in clear
        self.kr.set_password(self.slot, "current_token", _fake_encrypt(stub, "pass-1"))
        self.assertIn("idp_refresh_token#0", self.leaked())
        with patch("builtins.print"):
            self.mgr.encrypt_vault("pass-1")
        self.assertEqual(self.leaked(), [])
        self.assertEqual(self.mgr._get_token_meta(self.slot)["refresh_token"], "RT-SECRET")

    def test_encrypt_vault_leaves_a_token_it_cant_decrypt_as_it_is(self):
        """A side entry encrypted with another passphrase was read as "" and written back as an empty token, and
        not counted as a failure. Now the slot is left untouched and counted."""
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        idp._put_chunked(self.slot, "idp_refresh_token", _fake_encrypt("RT-OTHER", "other-pass"))
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
        self.kr.delete_password(self.slot, "idp_AccessToken#1")
        with patch("builtins.print") as printed:
            self.mgr.encrypt_vault("pass-1")
        self.assertIn("1 session(s)", " ".join(str(c.args[0]) for c in printed.call_args_list))
        self.assertIsNotNone(self.kr.get_password(self.slot, "idp_refresh_token#0"))

    def test_session_whose_token_cant_be_decrypted_is_not_valid(self):
        """_get_token_meta returned _is_valid True with an empty AccessToken when a side entry didn't decrypt."""
        self.encrypted_vault()
        self.mgr.touch_session(self.slot, dict(self.meta), "pilot")
        idp._put_chunked(self.slot, "idp_AccessToken", _fake_encrypt("AT-OTHER", "other-pass"))
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
        self.assertIsNone(getattr(self.mgr, "_pinned_vault_pass", None))   # nothing kept after the refresh

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
        self.assertTrue(self.kr.get_password(self.slot, "current_token").isdigit())
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
        self.assertTrue(self.kr.get_password(self.slot, "current_token").isdigit())
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
        self.assertTrue(self.kr.get_password(self.slot, "current_token").isdigit())
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
        self.assertTrue(self.kr.get_password(new_slot, "current_token").isdigit())
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


class TestVaultPromptMode(unittest.TestCase):
    """A typed vault passphrase is kept until the command ends (--vault-prompt once, the default, said once on stderr)
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
        self.assertEqual(err.count("kept in memory until this command ends"), 1)
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
        self.assertTrue(kr.get_password("slot", "current_token").isdigit())
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
