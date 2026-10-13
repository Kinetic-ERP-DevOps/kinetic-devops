"""AI server profile in the keyring (auth ai-set) and how analysis uses it.

Uses an in-memory fake keyring and a fake urlopen; no real keyring or network.
"""

import io
import json
import os
import unittest
from unittest import mock

from kinetic_devops import analysis, auth

# Fake credential for tests only (never a real key).
FAKE_KEY = "fake-test-key"


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, value):
        self.store[(service, user)] = value

    def delete_password(self, service, user):
        if (service, user) not in self.store:
            raise KeyError(user)
        del self.store[(service, user)]


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class AIProfileTestBase(unittest.TestCase):
    def setUp(self):
        self.kr = FakeKeyring()
        patcher = mock.patch.object(auth, "keyring", self.kr)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"KINETIC_AI_BASE_URL": ""})
        env.start()
        self.addCleanup(env.stop)


class TestProfileStorage(AIProfileTestBase):
    def test_set_get_and_key_kept_out_of_profile(self):
        auth.set_ai_profile("http://ai-host:3000/", "openwebui", "llama3", api_key=FAKE_KEY)
        profile = auth.get_ai_profile()
        self.assertEqual(profile, {"base_url": "http://ai-host:3000", "api_style": "openwebui", "model": "llama3"})
        self.assertNotIn(FAKE_KEY, self.kr.store[(auth.SERVICE_AI, "profile")])
        self.assertEqual(auth.get_ai_api_key("http://ai-host:3000/"), FAKE_KEY)

    def test_none_key_keeps_existing_key(self):
        auth.set_ai_profile("http://ai-host:3000", "openwebui", "", api_key="sk-1")
        auth.set_ai_profile("http://ai-host:3000", "openwebui", "other-model", api_key=None)
        self.assertEqual(auth.get_ai_api_key("http://ai-host:3000"), "sk-1")

    def test_rejects_bad_url_and_style(self):
        with self.assertRaises(ValueError):
            auth.set_ai_profile("ai-host:3000", "openwebui")
        with self.assertRaises(ValueError):
            auth.set_ai_profile("http://ai-host:3000", "bogus")

    def test_clear_removes_profile_and_key(self):
        auth.set_ai_profile("http://ai-host:3000", "openwebui", "", api_key="sk-1")
        self.assertTrue(auth.clear_ai_profile())
        self.assertEqual(auth.get_ai_profile(), {})
        self.assertEqual(auth.get_ai_api_key("http://ai-host:3000"), "")
        self.assertFalse(auth.clear_ai_profile())


class TestAnalysisUsesProfile(AIProfileTestBase):
    def _capture(self, payload):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            return _Response(json.dumps(payload).encode())

        return calls, mock.patch.object(analysis.request, "urlopen", side_effect=fake_urlopen)

    def test_base_url_falls_back_to_stored_profile(self):
        auth.set_ai_profile("http://ai-host:3000", "openwebui")
        self.assertEqual(analysis._resolve_ai_base_url(""), "http://ai-host:3000")
        self.assertEqual(analysis._resolve_ai_base_url("http://explicit:1234"), "http://explicit:1234")

    def test_stored_profile_beats_env_var_which_is_only_a_fallback(self):
        with mock.patch.dict(os.environ, {"KINETIC_AI_BASE_URL": "http://from-env:8080"}):
            self.assertEqual(analysis._resolve_ai_base_url(""), "http://from-env:8080")
            auth.set_ai_profile("http://ai-host:3000", "openwebui")
            self.assertEqual(analysis._resolve_ai_base_url(""), "http://ai-host:3000")
            self.assertEqual(analysis._resolve_ai_base_url("http://explicit:1234"), "http://explicit:1234")

    def test_missing_url_error_points_to_ai_set(self):
        with self.assertRaises(ValueError) as ctx:
            analysis._resolve_ai_base_url("")
        self.assertIn("ai-set", str(ctx.exception))

    def test_openwebui_requests_use_chat_api_and_bearer_key(self):
        auth.set_ai_profile("http://ai-host:3000", "openwebui", api_key="sk-1")
        calls, patch = self._capture({"data": [{"id": "m1"}], "choices": [{"message": {"content": " hi "}}]})
        with patch:
            self.assertEqual(analysis.fetch_models("http://ai-host:3000", 5), ["m1"])
            response = analysis.call_ai("http://ai-host:3000", 5, "m1", "prompt text")
        self.assertEqual(calls[0].full_url, "http://ai-host:3000/api/models")
        self.assertEqual(calls[1].full_url, "http://ai-host:3000/api/chat/completions")
        self.assertEqual(calls[1].get_header("Authorization"), "Bearer sk-1")
        body = json.loads(calls[1].data)
        self.assertEqual(body["messages"], [{"role": "user", "content": "prompt text"}])
        self.assertEqual(analysis.extract_text(response), "hi")

    def test_openai_style_unchanged_and_no_key_no_header(self):
        calls, patch = self._capture({"data": [{"id": "m1"}], "choices": [{"text": " ok "}]})
        with patch:
            analysis.fetch_models("http://plain:1234", 5)
            response = analysis.call_ai("http://plain:1234", 5, "m1", "p")
        self.assertEqual(calls[0].full_url, "http://plain:1234/v1/models")
        self.assertEqual(calls[1].full_url, "http://plain:1234/v1/completions")
        self.assertIsNone(calls[1].get_header("Authorization"))
        self.assertEqual(json.loads(calls[1].data)["prompt"], "p")
        self.assertEqual(analysis.extract_text(response), "ok")

    def test_stored_style_only_applies_to_its_own_url(self):
        auth.set_ai_profile("http://ai-host:3000", "openwebui")
        calls, patch = self._capture({"data": []})
        with patch:
            analysis.fetch_models("http://other:1234", 5)
        self.assertEqual(calls[0].full_url, "http://other:1234/v1/models")

    def test_select_model_prefers_stored_default_when_available(self):
        auth.set_ai_profile("http://ai-host:3000", "openwebui", "llama3")
        self.assertEqual(analysis.select_model(["a", "llama3"], "http://ai-host:3000"), "llama3")
        self.assertEqual(analysis.select_model(["a", "b"], "http://ai-host:3000"), "a")
        self.assertEqual(analysis.select_model(["a", "llama3"], "http://other:1"), "a")


if __name__ == "__main__":
    unittest.main()
