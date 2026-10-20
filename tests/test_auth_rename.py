"""auth rename: moves an environment and its cached sessions without re-entry.

Uses an in-memory fake keyring; the real keyring is never touched.
"""

import json
import os
import unittest
from unittest import mock

from kinetic_devops import auth, crypto
from kinetic_devops.auth import SERVICE_SERVERS, KineticConfigManager
from tests.cli_matrix import native_checks_enabled


class FakeKeyring:
    """dict-backed stand-in for the keyring module. case_insensitive mimics
    stores (like Windows Credential Manager) that match names ignoring case."""

    def __init__(self, case_insensitive=False):
        self.case_insensitive = case_insensitive
        self.store = {}

    def _key(self, service, user):
        return (service.lower(), user.lower()) if self.case_insensitive else (service, user)

    def get_password(self, service, user):
        return self.store.get(self._key(service, user))

    def set_password(self, service, user, value):
        self.store[self._key(service, user)] = value

    def delete_password(self, service, user):
        self.store.pop(self._key(service, user))


class RenameTestBase(unittest.TestCase):
    case_insensitive = False

    def setUp(self):
        self.kr = FakeKeyring(case_insensitive=self.case_insensitive)
        patcher = mock.patch.object(auth, "keyring", self.kr)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mgr = KineticConfigManager.__new__(KineticConfigManager)
        self.servers = {
            "OldEnv": {"url": "https://example.invalid/a", "companies": "CO1", "api_key": "key-1",
                       "sessions": ["alice", "bob"]},
            "Other": {"url": "https://example.invalid/b", "companies": "CO2", "api_key": "key-2", "sessions": []},
        }
        self.kr.set_password(SERVICE_SERVERS, "config", json.dumps(self.servers))
        self.alice_old = self.mgr._get_token_key("OldEnv", "alice", "key-1")
        self.kr.set_password(self.alice_old, "current_token", '{"AccessToken": "tok-a"}')
        self.kr.set_password("KineticSDK", "LAST_GLOBAL_SESSION", self.alice_old)

    def config(self):
        return json.loads(self.kr.get_password(SERVICE_SERVERS, "config"))


class TestRename(RenameTestBase):
    def test_moves_config_sessions_and_pointer(self):
        result = self.mgr.rename("oldenv", "NewEnv")
        config = self.config()
        self.assertEqual(list(config), ["NewEnv", "Other"], "position preserved, old key gone")
        self.assertEqual(config["NewEnv"], self.servers["OldEnv"])
        alice_new = self.mgr._get_token_key("NewEnv", "alice", "key-1")
        self.assertEqual(self.kr.get_password(alice_new, "current_token"), '{"AccessToken": "tok-a"}')
        self.assertIsNone(self.kr.get_password(self.alice_old, "current_token"))
        self.assertEqual(self.kr.get_password("KineticSDK", "LAST_GLOBAL_SESSION"), alice_new)
        self.assertEqual(result["sessions_moved"], ["alice"])
        self.assertEqual(result["sessions_without_cached_token"], ["bob"])
        self.assertTrue(result["pointer_moved"])

    def test_dry_run_changes_nothing(self):
        before = dict(self.kr.store)
        result = self.mgr.rename("OldEnv", "NewEnv", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.kr.store, before)

    def test_rejects_unknown_existing_and_invalid_names(self):
        with self.assertRaises(ValueError):
            self.mgr.rename("Missing", "NewEnv")
        with self.assertRaises(ValueError):
            self.mgr.rename("OldEnv", "other")
        with self.assertRaises(ValueError):
            self.mgr.rename("OldEnv", "has space")
        with self.assertRaises(ValueError):
            self.mgr.rename("OldEnv", "OldEnv")

    def test_pointer_to_another_environment_is_left_alone(self):
        self.kr.set_password("KineticSDK", "LAST_GLOBAL_SESSION", "Other-carol-abc")
        result = self.mgr.rename("OldEnv", "NewEnv")
        self.assertFalse(result["pointer_moved"])
        self.assertEqual(self.kr.get_password("KineticSDK", "LAST_GLOBAL_SESSION"), "Other-carol-abc")

    def test_failure_before_config_write_keeps_old_entry(self):
        original_set = self.kr.set_password

        def failing_set(service, user, value):
            if service == SERVICE_SERVERS:
                raise RuntimeError("keyring unavailable")
            return original_set(service, user, value)

        with mock.patch.object(self.kr, "set_password", side_effect=failing_set):
            with self.assertRaises(RuntimeError):
                self.mgr.rename("OldEnv", "NewEnv")
        self.assertIn("OldEnv", self.config())
        self.assertEqual(self.kr.get_password(self.alice_old, "current_token"), '{"AccessToken": "tok-a"}')


class TestCaseOnlyRenameOnCaseInsensitiveStore(RenameTestBase):
    case_insensitive = True

    def test_case_only_rename_keeps_the_token(self):
        self.mgr.rename("OldEnv", "OLDENV")
        self.assertIn("OLDENV", self.config())
        alice_new = self.mgr._get_token_key("OLDENV", "alice", "key-1")
        self.assertEqual(self.kr.get_password(alice_new, "current_token"), '{"AccessToken": "tok-a"}')


class TestEncryptedVault(RenameTestBase):
    def setUp(self):
        super().setUp()
        # Vault encryption loads cryptography's native library, which endpoint
        # security may block on a development machine: a native check, so it
        # runs in CI, Windows Sandbox or on request, and isn't even attempted otherwise.
        if not native_checks_enabled():
            self.skipTest("native check deferred (loads cryptography's native library; runs in CI, "
                          "scripts/sandbox_test.py, or tests.test_runner --native)")
        try:
            blob = crypto.encrypt_json(self.servers, "pass-1")
        except Exception as exc:  # cryptography not installed, or not loadable
            self.skipTest(f"vault encryption unavailable: {exc}")
        self.kr.set_password(SERVICE_SERVERS, "config", blob)

    def test_vault_stays_encrypted_with_same_passphrase(self):
        with mock.patch.dict(os.environ, {"KINETIC_VAULT_PASSPHRASE": "pass-1"}):
            self.mgr.rename("OldEnv", "NewEnv")
        raw = self.kr.get_password(SERVICE_SERVERS, "config")
        self.assertTrue(crypto.is_encrypted_blob(raw))
        self.assertIn("NewEnv", crypto.decrypt_json(raw, "pass-1"))



class TestEncryptedVaultWithoutPassphrase(RenameTestBase):
    """Needs no real encryption: any {"_enc": true} wrapper counts as a vault."""

    def test_missing_passphrase_fails_without_changes(self):
        self.kr.set_password(SERVICE_SERVERS, "config", json.dumps({"_enc": True, "data": "opaque"}))
        before = dict(self.kr.store)
        with mock.patch.dict(os.environ, {"KINETIC_VAULT_PASSPHRASE": ""}), \
                mock.patch.object(auth.sys.stdout, "isatty", return_value=False):
            with self.assertRaises(RuntimeError):
                self.mgr.rename("OldEnv", "NewEnv")
        self.assertEqual(self.kr.store, before)


if __name__ == "__main__":
    unittest.main()
