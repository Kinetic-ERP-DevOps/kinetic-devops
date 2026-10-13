"""Regression tests for scripts/tax_clear.py."""

import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


def _load_tax_clear_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "tax_clear.py"
    spec = importlib.util.spec_from_file_location("tax_clear_script", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load tax_clear.py")

    module = importlib.util.module_from_spec(spec)
    fake_keyring = types.ModuleType("keyring")
    fake_keyring.get_password = lambda *args, **kwargs: None
    fake_keyring.set_password = lambda *args, **kwargs: None
    fake_keyring.delete_password = lambda *args, **kwargs: None

    with patch.dict(sys.modules, {"keyring": fake_keyring}):
        with patch("logging.FileHandler", side_effect=lambda *args, **kwargs: logging.NullHandler()):
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)

    return module


class TestTaxClear(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_tax_clear_module()

    def test_clear_company_tax_configs_resolves_active_config_before_update(self):
        mgr = MagicMock()
        mgr.get_active_config.return_value = ("https://example", "token", "api-key")

        with patch.object(self.mod, "KineticConfigManager", return_value=mgr) as manager_cls:
            with patch.object(self.mod, "TaxService") as tax_service_cls:
                tax_service = tax_service_cls.return_value
                tax_service.get_inactive_configs.return_value = [{"SysRowID": "1"}]
                tax_service.update_configs.return_value = True

                success = self.mod.clear_company_tax_configs("DEV", "EPIC06", inactive_only=True)

        self.assertTrue(success)
        manager_cls.assert_called_once_with(debug=False)
        mgr.get_active_config.assert_called_once_with(
            {"nickname": "DEV"},
            fields=("url", "token", "api_key"),
        )
        tax_service_cls.assert_called_once_with("https://example", "token", "api-key", debug=False)
        tax_service.update_configs.assert_called_once_with("EPIC06", [{"SysRowID": "1"}])


if __name__ == "__main__":
    unittest.main()
