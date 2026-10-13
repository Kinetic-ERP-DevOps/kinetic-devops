"""Regression tests for `auth.py use <env>` when nothing usable is stored.

Covers two failure modes that used to be silent or unhelpful:
- no keyring backend at all (headless Linux): raw NoKeyringError traceback
  from KineticCore's salt lookup during construction.
- an empty or non-matching store: `use` printed an error but returned
  normally, so the CLI exited 0 and looked like a successful switch.
"""

import unittest
from unittest.mock import patch

import keyring.errors

from kinetic_devops.auth import KineticConfigManager, SessionResolutionError


def _bare_manager() -> KineticConfigManager:
    """Construct a KineticConfigManager without running __init__ (which
    touches the keyring for salt derivation)."""
    return KineticConfigManager.__new__(KineticConfigManager)


class TestNoKeyringBackend(unittest.TestCase):
    def test_missing_backend_raises_actionable_error(self):
        with patch("kinetic_devops.KineticCore.keyring.get_password",
                   side_effect=keyring.errors.NoKeyringError("no backend")):
            with self.assertRaises(RuntimeError) as ctx:
                KineticConfigManager()

        self.assertNotIsInstance(ctx.exception, keyring.errors.NoKeyringError)
        self.assertIn("No keyring backend available", str(ctx.exception))


class TestUseWithoutStoredEnv(unittest.TestCase):
    def test_empty_store_raises_and_points_to_store(self):
        mgr = _bare_manager()
        with patch.object(mgr, "_get_server_dict", return_value={}):
            with self.assertRaises(SessionResolutionError) as ctx:
                mgr.use("pilot")

        self.assertIn("pilot", str(ctx.exception))
        self.assertIn("Run 'store' first", str(ctx.exception))

    def test_unknown_env_raises_and_lists_stored_envs(self):
        mgr = _bare_manager()
        servers = {"third": {"url": "https://third.example", "sessions": []}}
        with patch.object(mgr, "_get_server_dict", return_value=servers):
            with self.assertRaises(SessionResolutionError) as ctx:
                mgr.use("pilot")

        self.assertIn("no stored environment by that name", str(ctx.exception))
        self.assertIn("third", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
