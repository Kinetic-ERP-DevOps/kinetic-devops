"""Tests for kinetic_devops.idp (Epicor IdP sign-in) and its use in auth. No network: requests is mocked."""

import base64
import hashlib
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

from kinetic_devops import idp
from kinetic_devops.auth import KineticConfigManager

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
    def _run(self, reply, timeout=10, claims=None):
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
            tok = idp.login_browser(SETTINGS, "cid", port=port, timeout=timeout, open_url=browser)
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

    def test_lock_waits_and_times_out_when_the_lock_cant_be_checked(self):
        """Regression: an OSError while checking a held lock used to retry at once, forever (no sleep, no deadline)."""
        path = os.path.join(idp.lock_dir(), "kinetic-idp-" + hashlib.sha256(self.slot.encode()).hexdigest()[:16] + ".lock")
        open(path, "w").close()
        outcome = []

        def worker():
            try:
                with patch.object(idp.os.path, "getmtime", side_effect=OSError("gone")):
                    with idp.slot_lock(self.slot, wait=0.5):
                        outcome.append("locked")
            except idp.IdPError as exc:
                outcome.append(str(exc))
        try:
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            t.join(5)
            self.assertFalse(t.is_alive(), "slot_lock spun past its deadline")
            self.assertIn("Timed out", outcome[0])
        finally:
            os.remove(path)

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
