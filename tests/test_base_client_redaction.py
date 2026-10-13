import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from kinetic_devops.KineticCore import KineticCore
from kinetic_devops.base_client import KineticBaseClient


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", reason="OK", request_headers=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.reason = reason
        self.ok = status_code < 400
        self.request = MagicMock(headers=request_headers or {})

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(f"{self.status_code} {self.reason}")


class SafeCore(KineticCore):
    def __init__(self, debug=True):
        self.debug = debug


class TestWireRedaction(unittest.TestCase):
    def test_log_wire_redacts_url_headers_and_body(self):
        core = SafeCore(debug=True)
        response = FakeResponse(status_code=400, json_data={"Company": "TENANT_ID", "detail": "see https://tenant.example.invalid/api"}, reason="Bad Request")

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            core.log_wire(
                "POST",
                "https://tenant.example.invalid/api/v2/odata/TENANT_ID/Ice.BO.UserFileSvc/UserFiles?$filter=UserID eq 'alice'",
                {
                    "Authorization": "Bearer REAL_TOKEN",
                    "X-API-Key": "REAL_API_KEY",
                    "X-Company": "TENANT_ID",
                    "Accept": "application/json",
                },
                body={"Company": "TENANT_ID", "Password": "super-secret", "endpoint": "https://tenant.example.invalid/private"},
                resp=response,
            )

        output = stdout.getvalue()
        self.assertIn("URL: https://[REDACTED_HOST]/api/v2/odata/[REDACTED_COMPANY]/Ice.BO.UserFileSvc/UserFiles?[REDACTED_QUERY]", output)
        self.assertIn("Bearer [REDACTED]", output)
        self.assertIn('"X-API-Key": "[REDACTED]"', output)
        self.assertIn('"X-Company": "[REDACTED]"', output)
        self.assertIn('"Password": "[REDACTED]"', output)
        self.assertIn('"Company": "[REDACTED]"', output)
        self.assertIn("[REDACTED_URL]", output)
        self.assertNotIn("tenant.example.invalid", output)
        self.assertNotIn("REAL_TOKEN", output)
        self.assertNotIn("REAL_API_KEY", output)
        self.assertNotIn("super-secret", output)
        self.assertNotIn("alice", output)
        self.assertNotIn("TENANT_ID", output)

    @patch("kinetic_devops.base_client.requests.request")
    def test_execute_request_updates_session_without_logging_on_success(self, request_mock):
        request_mock.return_value = FakeResponse(
            status_code=200,
            json_data={"ok": True},
            request_headers={"Authorization": "Bearer REAL_TOKEN", "X-Epicor-Company": "TENANT_ID"},
        )

        client = KineticBaseClient.__new__(KineticBaseClient)
        client.debug = False
        client.mgr = MagicMock()
        client.config = {
            "url": "https://tenant.example.invalid",
            "token": "REAL_TOKEN",
            "api_key": "REAL_API_KEY",
            "company": "TENANT_ID",
            "nickname": "ENV",
            "user_id": "alice",
        }

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = client.execute_request("GET", "https://tenant.example.invalid/api/v2/odata/TENANT_ID/Ice.BO.PingSvc/Pings")

        self.assertEqual(result, {"ok": True})
        client.mgr.touch_from_headers.assert_called_once_with({"Authorization": "Bearer REAL_TOKEN", "X-Epicor-Company": "TENANT_ID"})
        self.assertEqual(stdout.getvalue(), "")

    @patch("kinetic_devops.base_client.requests.request")
    def test_execute_request_failure_logs_redacted_output(self, request_mock):
        request_mock.return_value = FakeResponse(
            status_code=500,
            json_data={"Company": "TENANT_ID", "message": "user alice failed against https://tenant.example.invalid/internal"},
            reason="Server Error",
            request_headers={"Authorization": "Bearer REAL_TOKEN", "X-Epicor-Company": "TENANT_ID"},
        )

        client = KineticBaseClient.__new__(KineticBaseClient)
        client.debug = False
        client.mgr = MagicMock()
        client.config = {
            "url": "https://tenant.example.invalid",
            "token": "REAL_TOKEN",
            "api_key": "REAL_API_KEY",
            "company": "TENANT_ID",
            "nickname": "ENV",
            "user_id": "alice",
        }

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            with self.assertRaises(Exception):
                client.execute_request(
                    "POST",
                    "https://tenant.example.invalid/api/v2/odata/TENANT_ID/Ice.BO.UserFileSvc/UserFiles?$filter=UserID eq 'alice'",
                    payload={"Password": "super-secret", "Company": "TENANT_ID"},
                )

        output = stdout.getvalue()
        self.assertIn("WIRE LOG (REDACTED)", output)
        self.assertIn("[REDACTED_HOST]", output)
        self.assertIn('"Password": "[REDACTED]"', output)
        self.assertIn('"Company": "[REDACTED]"', output)
        self.assertNotIn("tenant.example.invalid", output)
        self.assertNotIn("REAL_TOKEN", output)
        self.assertNotIn("REAL_API_KEY", output)
        self.assertNotIn("super-secret", output)
        self.assertNotIn("TENANT_ID", output)
        self.assertNotIn("alice", output)
        client.mgr.touch_from_headers.assert_not_called()


if __name__ == "__main__":
    unittest.main()