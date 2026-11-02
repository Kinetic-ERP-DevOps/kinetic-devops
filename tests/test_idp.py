"""Tests for kinetic_devops.idp (Epicor IdP sign-in) and its use in auth. No network: requests is mocked."""

import socket
import sys
import threading
import time
import unittest
import urllib.parse
import urllib.request
from unittest.mock import MagicMock, patch

from kinetic_devops import idp
from kinetic_devops.auth import KineticConfigManager

CONFIG = {"IdentityProvider": {"Enabled": True, "Endpoint": "https://idp.example/tid/42/",
                               "ApiScope": "epicor_erp idp_access"}}
DISCOVERY = {"authorization_endpoint": "https://idp.example/tid/42/connect/authorize",
             "token_endpoint": "https://idp.example/tid/42/connect/token"}
SETTINGS = {"endpoint": "https://idp.example/tid/42", "tenant": "42", "authorize": DISCOVERY["authorization_endpoint"],
            "token": DISCOVERY["token_endpoint"], "api_scope": "epicor_erp idp_access"}


def _resp(json_body, status=200):
    r = MagicMock()
    r.ok = 200 <= status < 300
    r.status_code = status
    r.json.return_value = json_body
    r.headers = {"content-type": "application/json"}
    return r


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestDiscover(unittest.TestCase):
    def test_reads_tenant_endpoints_and_scope(self):
        req = MagicMock()
        req.get.side_effect = [_resp(CONFIG), _resp(DISCOVERY)]
        with patch.object(idp, "requests", req):
            s = idp.discover("https://kinetic.example/Env/")
        self.assertEqual(s, SETTINGS)
        self.assertEqual(req.get.call_args_list[0].args[0], "https://kinetic.example/Env/api/.configuration")

    def test_disabled_idp_raises(self):
        req = MagicMock()
        req.get.return_value = _resp({"IdentityProvider": {"Enabled": False}})
        with patch.object(idp, "requests", req), self.assertRaises(idp.IdPError):
            idp.discover("https://kinetic.example/Env")


class TestLoginBrowser(unittest.TestCase):
    def _run(self, reply):
        """reply(params) -> query dict the 'browser' sends back to the loopback redirect."""
        port = _free_port()

        def browser(url):
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            self.seen = params

            def go():
                time.sleep(0.2)
                q = urllib.parse.urlencode(reply(params))
                urllib.request.urlopen(f"{params['redirect_uri']}?{q}", timeout=5).read()
            threading.Thread(target=go, daemon=True).start()
            return True

        req = MagicMock()
        req.post.return_value = _resp({"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        with patch.object(idp, "requests", req):
            tok = idp.login_browser(SETTINGS, "cid", port=port, timeout=10, open_url=browser)
        return tok, req, port

    def test_pkce_code_exchange(self):
        tok, req, port = self._run(lambda p: {"code": "C1", "state": p["state"]})
        self.assertEqual(tok["access_token"], "AT")
        self.assertEqual(self.seen["code_challenge_method"], "S256")
        self.assertEqual(self.seen["redirect_uri"], f"http://127.0.0.1:{port}/callback")
        self.assertIn("offline_access", self.seen["scope"])
        self.assertIn("epicor_erp", self.seen["scope"])
        data = req.post.call_args.kwargs["data"]
        self.assertEqual((data["grant_type"], data["code"], data["client_id"]), ("authorization_code", "C1", "cid"))
        self.assertTrue(data["code_verifier"])

    def test_state_mismatch_is_refused(self):
        with self.assertRaises(idp.IdPError):
            self._run(lambda p: {"code": "C1", "state": "forged"})

    def test_idp_error_is_reported(self):
        with self.assertRaises(idp.IdPError) as ctx:
            self._run(lambda p: {"error": "access_denied", "state": p["state"]})
        self.assertIn("access_denied", str(ctx.exception))


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

    def test_failed_refresh_returns_none_and_stores_nothing(self):
        tok, _ = self._refresh(lambda: dict(self.meta), post=_resp({"error": "invalid_grant"}, 400))
        self.assertIsNone(tok)
        self.assertEqual(self.stored, [])

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
