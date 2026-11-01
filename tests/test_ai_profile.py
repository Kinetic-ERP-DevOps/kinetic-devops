"""AI server profile in the keyring (auth ai-set) and how analysis uses it.

Uses an in-memory fake keyring and a fake urlopen; no real keyring or network.
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
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
        env = mock.patch.dict(os.environ, {"KINETIC_AI_BASE_URL": "", "KINETIC_AI_PROFILE": ""})
        env.start()
        self.addCleanup(env.stop)
        analysis.set_active_ai_profile(None)
        self.addCleanup(analysis.set_active_ai_profile, None)


class TestProfileStorage(AIProfileTestBase):
    def test_set_get_and_key_kept_out_of_profile(self):
        auth.set_ai_profile("http://ai-host:3000/", "openwebui", "llama3", api_key=FAKE_KEY)
        profile = auth.get_ai_profile()
        self.assertEqual(profile, {"base_url": "http://ai-host:3000", "api_style": "openwebui", "model": "llama3"})
        self.assertNotIn(FAKE_KEY, self.kr.store[(auth.SERVICE_AI, "profiles")])
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

    def test_rejects_urls_with_credentials_query_fragment_or_no_host(self):
        for bad in (
            "https://user:pass@ai-host:3000",
            "https://user@ai-host",
            "https://ai-host:3000?token=x",
            "https://ai-host:3000/#frag",
            "https://ai-host:3000/?",
            "https://",
            "http://ai-host:notaport",
            "ftp://ai-host",
        ):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                auth.set_ai_profile(bad, "openwebui")
        self.assertEqual(auth.list_ai_profiles(), ("", {}))
        profile = auth.set_ai_profile("https://ai-host:3000/api/", "openwebui")
        self.assertEqual(profile["base_url"], "https://ai-host:3000/api")

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



class TestNamedProfiles(AIProfileTestBase):
    URL = "http://ai-host:3000"

    def test_profiles_on_one_server_share_a_key_and_first_is_default(self):
        auth.set_ai_profile(self.URL, "openwebui", "local/deep", api_key=FAKE_KEY, name="deep")
        auth.set_ai_profile(self.URL, "openwebui", "local/fast", name="fast")
        default, profiles = auth.list_ai_profiles()
        self.assertEqual(default, "deep")
        self.assertEqual(sorted(profiles), ["deep", "fast"])
        self.assertEqual(auth.get_ai_profile("fast")["model"], "local/fast")
        self.assertEqual(auth.get_ai_api_key(self.URL), FAKE_KEY)
        self.assertNotIn(FAKE_KEY, self.kr.store[(auth.SERVICE_AI, "profiles")])

    def test_make_default_and_set_default(self):
        auth.set_ai_profile(self.URL, "openwebui", name="a")
        auth.set_ai_profile(self.URL, "openwebui", name="b", make_default=True)
        self.assertEqual(auth.list_ai_profiles()[0], "b")
        auth.set_default_ai_profile("a")
        self.assertEqual(auth.list_ai_profiles()[0], "a")
        with self.assertRaises(ValueError):
            auth.set_default_ai_profile("missing")

    def test_clearing_one_profile_keeps_a_key_still_in_use(self):
        auth.set_ai_profile(self.URL, "openwebui", api_key=FAKE_KEY, name="deep")
        auth.set_ai_profile(self.URL, "openwebui", name="fast")
        self.assertTrue(auth.clear_ai_profile("deep"))
        self.assertEqual(auth.get_ai_api_key(self.URL), FAKE_KEY)
        self.assertEqual(auth.list_ai_profiles()[0], "fast")
        self.assertTrue(auth.clear_ai_profile("fast"))
        self.assertEqual(auth.get_ai_api_key(self.URL), "")
        self.assertEqual(auth.list_ai_profiles(), ("", {}))

    def test_legacy_single_profile_is_migrated(self):
        legacy = {"base_url": self.URL, "api_style": "openwebui", "model": "m"}
        self.kr.set_password(auth.SERVICE_AI, "profile", json.dumps(legacy))
        self.assertEqual(auth.list_ai_profiles(), ("default", {"default": legacy}))
        auth.set_ai_profile(self.URL, "openwebui", "m2", name="fast")
        self.assertIsNone(self.kr.get_password(auth.SERVICE_AI, "profile"))
        self.assertEqual(sorted(auth.list_ai_profiles()[1]), ["default", "fast"])

    def test_profile_name_validation(self):
        with self.assertRaises(ValueError):
            auth.set_ai_profile(self.URL, "openwebui", name="has space")

    def test_analysis_uses_selected_profile_and_rejects_unknown(self):
        auth.set_ai_profile(self.URL, "openwebui", "local/deep", name="deep")
        auth.set_ai_profile("http://other:1234", "openai", "big", name="other")
        models = ["local/fast", "local/deep"]
        self.assertEqual(analysis.select_model(models, analysis._resolve_ai_base_url("")), "local/deep")
        analysis.set_active_ai_profile("other")
        self.assertEqual(analysis._resolve_ai_base_url(""), "http://other:1234")
        self.assertEqual(analysis._ai_client_settings("http://other:1234")[0], "openai")
        with self.assertRaises(ValueError):
            analysis.set_active_ai_profile("missing")
        # A failed selection keeps the previous valid one.
        self.assertEqual(analysis._resolve_ai_base_url(""), "http://other:1234")

    def test_explicit_url_gets_style_from_matching_profile(self):
        auth.set_ai_profile(self.URL, "openwebui", name="deep")
        auth.set_ai_profile("http://other:1234", "openai", name="other")
        self.assertEqual(analysis._ai_client_settings(self.URL)[0], "openwebui")


class TestProfilePackage(AIProfileTestBase):
    """One self-contained JSON entry per profile, and the KINETIC_AI_PROFILE equivalent."""

    URL = "http://ai-host:3000"

    def entry(self, name):
        return json.loads(self.kr.get_password(auth.SERVICE_AI, "profile:" + name))

    def test_profile_entry_is_self_contained(self):
        auth.set_ai_profile(self.URL, "openwebui", "local/deep", api_key=FAKE_KEY, name="deep")
        self.assertEqual(self.entry("deep"), {"v": 1, "base_url": self.URL, "api_style": "openwebui",
                                              "model": "local/deep", "api_key": FAKE_KEY})
        index = json.loads(self.kr.get_password(auth.SERVICE_AI, "profiles"))
        self.assertEqual(index, {"v": 2, "default": "deep", "names": ["deep"]})
        self.assertNotIn(FAKE_KEY, json.dumps(index))
        self.assertNotIn("api_key", auth.list_ai_profiles()[1]["deep"])
        self.assertNotIn("api_key", auth.get_ai_profile("deep"))

    def test_new_key_rotates_every_profile_on_that_server(self):
        auth.set_ai_profile(self.URL, "openwebui", name="deep", api_key="old")
        auth.set_ai_profile(self.URL, "openwebui", name="fast")
        self.assertEqual(self.entry("fast")["api_key"], "old")
        auth.set_ai_profile(self.URL, "openwebui", name="fast", api_key="new")
        self.assertEqual(self.entry("deep")["api_key"], "new")

    def test_previous_layout_is_migrated_and_cleaned_up(self):
        old = {"default": "deep", "profiles": {"deep": {"base_url": self.URL, "api_style": "openwebui", "model": "m"}}}
        self.kr.set_password(auth.SERVICE_AI, "profiles", json.dumps(old))
        self.kr.set_password(auth.SERVICE_AI, self.URL, FAKE_KEY)
        self.assertEqual(auth.get_ai_api_key(self.URL), FAKE_KEY)
        auth.set_default_ai_profile("deep")  # any change writes the new layout
        self.assertEqual(self.entry("deep")["api_key"], FAKE_KEY)
        self.assertIsNone(self.kr.get_password(auth.SERVICE_AI, self.URL))

    def test_clear_removes_the_profile_entry(self):
        auth.set_ai_profile(self.URL, "openwebui", name="a", api_key=FAKE_KEY)
        auth.set_ai_profile(self.URL, "openwebui", name="b")
        auth.clear_ai_profile("a")
        self.assertIsNone(self.kr.get_password(auth.SERVICE_AI, "profile:a"))
        auth.clear_ai_profile()
        self.assertEqual(self.kr.store, {})

    def test_env_profile_inline_supplies_url_style_model_and_key(self):
        package = {"v": 1, "base_url": "http://runner-ai:4000/", "api_style": "openai", "model": "m", "api_key": FAKE_KEY}
        with mock.patch.dict(os.environ, {auth.AI_PROFILE_ENV: json.dumps(package)}):
            self.assertEqual(analysis._resolve_ai_base_url(""), "http://runner-ai:4000")
            style, headers = analysis._ai_client_settings("http://runner-ai:4000")
            self.assertEqual((style, headers), ("openai", {"Authorization": f"Bearer {FAKE_KEY}"}))
            self.assertEqual(analysis.select_model(["x", "m"], "http://runner-ai:4000"), "m")
            self.assertNotIn("api_key", auth.get_ai_profile())

    def test_env_profile_from_file_and_named_profile_wins(self):
        import tempfile
        auth.set_ai_profile(self.URL, "openwebui", "local/deep", name="deep")
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "ai.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"base_url": "https://gw.example/v1", "api_key": FAKE_KEY}, fh)
            with mock.patch.dict(os.environ, {auth.AI_PROFILE_ENV: path}):
                self.assertEqual(auth.get_ai_profile()["base_url"], "https://gw.example/v1")
                self.assertEqual(auth.get_ai_profile()["api_style"], "openai")
                self.assertEqual(auth.get_ai_api_key("https://gw.example/v1/"), FAKE_KEY)
                analysis.set_active_ai_profile("deep")
                self.assertEqual(analysis._resolve_ai_base_url(""), self.URL)

    def test_budget_is_part_of_the_profile_package(self):
        auth.set_ai_profile(self.URL, "openai-chat", "m", api_key=FAKE_KEY, name="big",
                            budget={"max_diff_chars": 120000, "max_output_tokens": 1500})
        self.assertEqual(self.entry("big")["budget"], {"max_diff_chars": 120000, "max_output_tokens": 1500})
        self.assertEqual(auth.get_ai_profile("big")["budget"]["max_output_tokens"], 1500)
        auth.set_ai_profile(self.URL, "openai-chat", "m", name="big")  # editing without a budget drops it
        self.assertNotIn("budget", self.entry("big"))

    def test_env_profile_carries_budget_and_rejects_bad_ones(self):
        package = {"base_url": "http://runner-ai:4000", "api_style": "openai-chat", "budget": {"max_diff_chars": 24000}}
        with mock.patch.dict(os.environ, {auth.AI_PROFILE_ENV: json.dumps(package)}):
            self.assertEqual(auth.get_ai_profile()["budget"], {"max_diff_chars": 24000})
        for bad in ({"max_diff_chars": 0}, {"bogus": 1}, [1]):
            with self.subTest(bad=bad), mock.patch.dict(
                os.environ, {auth.AI_PROFILE_ENV: json.dumps({"base_url": "http://x:1", "budget": bad})}
            ):
                with self.assertRaises(ValueError):
                    auth.get_ai_profile()

    def test_bad_env_profile_fails_loudly(self):
        for bad in ("{not json", '{"base_url": "ftp://x"}', '{"base_url": "http://x", "api_style": "bogus"}', "/no/such/file.json"):
            with self.subTest(bad=bad), mock.patch.dict(os.environ, {auth.AI_PROFILE_ENV: bad}):
                with self.assertRaises(ValueError):
                    analysis._resolve_ai_base_url("")


class TestReviewBudget(AIProfileTestBase):
    """Review budget (diff size, answer length) per AI profile; defaults unchanged."""

    URL = "http://gpu-host:4000"

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"KINETIC_AI_MAX_DIFF_CHARS": "", "KINETIC_AI_MAX_TOKENS": ""})
        env.start()
        self.addCleanup(env.stop)

    def _capture(self, payload):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            return _Response(json.dumps(payload).encode())

        return calls, mock.patch.object(analysis.request, "urlopen", side_effect=fake_urlopen)

    def test_defaults_match_the_small_preset_and_previous_hard_coded_values(self):
        self.assertEqual(
            analysis.resolve_ai_budget(self.URL),
            {"max_diff_chars": 4500, "max_output_tokens": 360, "retry_output_tokens": 220, "max_chunks": 20},
        )
        small = auth.AI_BUDGET_PRESETS["small"]
        self.assertEqual(small["max_diff_chars"], analysis.DEFAULT_MAX_DIFF_CHARS)
        self.assertEqual(small["max_output_tokens"], analysis.DEFAULT_MAX_OUTPUT_TOKENS)

    def test_profile_without_budget_is_stored_exactly_as_before(self):
        profile = auth.set_ai_profile(self.URL, "openai", "m")
        self.assertEqual(profile, {"base_url": self.URL, "api_style": "openai", "model": "m"})
        self.assertNotIn("budget", auth.get_ai_profile())

    def test_preset_budget_is_stored_and_used(self):
        auth.set_ai_profile(self.URL, "openai-chat", "local/deep", budget=auth.AI_BUDGET_PRESETS["large"])
        self.assertEqual(auth.get_ai_profile()["budget"], {"max_diff_chars": 120000, "max_output_tokens": 1500})
        budget = analysis.resolve_ai_budget(self.URL)
        self.assertEqual((budget["max_diff_chars"], budget["max_output_tokens"]), (120000, 1500))
        self.assertEqual(budget["retry_output_tokens"], round(1500 * 220 / 360))

    def test_budget_precedence_explicit_then_profile_then_env_then_default(self):
        with mock.patch.dict(os.environ, {"KINETIC_AI_MAX_DIFF_CHARS": "9000", "KINETIC_AI_MAX_TOKENS": "500"}):
            self.assertEqual(analysis.resolve_ai_budget(self.URL)["max_diff_chars"], 9000)
            auth.set_ai_profile(self.URL, "openai", budget={"max_diff_chars": 24000})
            budget = analysis.resolve_ai_budget(self.URL)
            self.assertEqual(budget["max_diff_chars"], 24000)   # profile beats env
            self.assertEqual(budget["max_output_tokens"], 500)  # not in profile -> env
            budget = analysis.resolve_ai_budget(self.URL, max_diff_chars=1000, max_output_tokens=50)
            self.assertEqual((budget["max_diff_chars"], budget["max_output_tokens"]), (1000, 50))

    def test_budget_of_another_server_is_not_applied(self):
        auth.set_ai_profile(self.URL, "openai", budget=auth.AI_BUDGET_PRESETS["large"])
        self.assertEqual(analysis.resolve_ai_budget("http://other:1")["max_diff_chars"], 4500)

    def test_invalid_budgets_are_rejected(self):
        for bad in ({"max_diff_chars": 0}, {"max_output_tokens": -5}, {"max_diff_chars": "big"},
                    {"max_diff_chars": True}, {"context": 4096}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                auth.set_ai_profile(self.URL, "openai", budget=bad)
        with mock.patch.dict(os.environ, {"KINETIC_AI_MAX_TOKENS": "zero"}), self.assertRaises(ValueError):
            analysis.resolve_ai_budget(self.URL)

    def test_openai_chat_style_uses_chat_completions(self):
        auth.set_ai_profile(self.URL, "openai-chat", api_key="sk-2")
        calls, patch = self._capture({"data": [{"id": "m1"}], "choices": [{"message": {"content": " ok "}}]})
        with patch:
            self.assertEqual(analysis.fetch_models(self.URL, 5), ["m1"])
            response = analysis.call_ai(self.URL, 5, "m1", "review this", max_tokens=1500)
        self.assertEqual(calls[0].full_url, f"{self.URL}/v1/models")
        self.assertEqual(calls[1].full_url, f"{self.URL}/v1/chat/completions")
        self.assertEqual(calls[1].get_header("Authorization"), "Bearer sk-2")
        body = json.loads(calls[1].data)
        self.assertEqual(body["messages"], [{"role": "user", "content": "review this"}])
        self.assertEqual(body["max_tokens"], 1500)
        self.assertNotIn("prompt", body)
        self.assertEqual(analysis.extract_text(response), "ok")

    def test_truncation_uses_the_budget(self):
        text = "x" * 10000
        self.assertEqual(analysis.truncate_text(text), "x" * 4500 + "\n...[truncated]")
        self.assertEqual(analysis.truncate_text(text, 20000), text)

    def test_prompt_policy_root_follows_active_root(self):
        item = {"id": "abc", "type": "commit", "payload": "diff"}
        with mock.patch.dict(os.environ, {"KINETIC_ACTIVE_ROOT": ""}):
            os.environ.pop("KINETIC_ACTIVE_ROOT")
            self.assertIn("Policy context: D:/Kinetic_SDK is active", analysis.build_prompt("r", "HEAD~1", item))
        with mock.patch.dict(os.environ, {"KINETIC_ACTIVE_ROOT": "/srv/kinetic"}):
            self.assertIn("Policy context: /srv/kinetic is active", analysis.build_prompt("r", "HEAD~1", item))


def _file_diff(name: str, hunks: int, lines_per_hunk: int = 5) -> str:
    out = [f"diff --git a/{name} b/{name}\n", f"--- a/{name}\n", f"+++ b/{name}\n"]
    for h in range(hunks):
        out.append(f"@@ -{h * 10 + 1},{lines_per_hunk} +{h * 10 + 1},{lines_per_hunk} @@\n")
        out += [f"+{name} hunk {h} line {i}\n" for i in range(lines_per_hunk)]
    return "".join(out)


class TestDiffChunking(unittest.TestCase):
    """Small budgets review the whole diff in chunks instead of truncating it."""

    def _content_lines(self, text):
        return [l for l in text.splitlines() if l.startswith("+") and not l.startswith("+++")]

    def test_patch_that_fits_is_one_unchanged_chunk(self):
        patch = _file_diff("a.py", 2)
        self.assertEqual(analysis.split_diff_chunks(patch, 10000), [patch])

    def test_splits_at_file_boundaries_and_loses_nothing(self):
        patch = "".join(_file_diff(f"f{i}.py", 3) for i in range(6))
        chunks = analysis.split_diff_chunks(patch, 1200)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1200 for c in chunks))
        self.assertTrue(all(c.startswith("diff --git ") for c in chunks))
        self.assertEqual(sum((self._content_lines(c) for c in chunks), []), self._content_lines(patch))

    def test_large_file_splits_at_hunks_and_keeps_its_header(self):
        patch = _file_diff("big.py", 20)
        chunks = analysis.split_diff_chunks(patch, 800)
        self.assertGreater(len(chunks), 2)
        for c in chunks:
            self.assertLessEqual(len(c), 800)
            self.assertTrue(c.startswith("diff --git a/big.py b/big.py\n"), c[:60])
            self.assertIn("@@ ", c)
        self.assertEqual(sum((self._content_lines(c) for c in chunks), []), self._content_lines(patch))

    def test_overlong_hunk_and_line_are_hard_split(self):
        patch = _file_diff("min.js", 1, 0) + "+" + "x" * 5000 + "\n"
        chunks = analysis.split_diff_chunks(patch, 1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual(sum(c.count("x") for c in chunks), 5000)

    def test_verdict_parsing(self):
        cases = {
            "...\n6) GO/NO-GO for further promotion\n**GO**. Safe.": "GO",
            "6) GO/NO-GO\n**NO-GO** (Conditional): fix first": "NO-GO",
            "## GO/NO-GO\nNo go until tests pass": "NO-GO",
            "6) GO/NO-GO\nUndecided.": "UNKNOWN",
            "short answer without sections: GO": "GO",
            "VERDICT: NO-GO\n1) Change Classification\n... 3) Regression Risk: cut off mid-sent": "NO-GO",
            "**VERDICT: GO**\n1) Change Classification ...": "GO",
            "1) Change Classification\n2) Functionality Loss Risk: the answer was cut off here": "UNKNOWN",
            "": "UNKNOWN",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(analysis.parse_verdict(text), expected)

    def test_combined_verdict(self):
        self.assertEqual(analysis.combine_verdicts(["GO", "GO"]), "GO")
        self.assertEqual(analysis.combine_verdicts(["GO", "NO-GO", "UNKNOWN"]), "NO-GO")
        self.assertEqual(analysis.combine_verdicts(["GO", "UNKNOWN"]), "UNKNOWN")
        self.assertEqual(analysis.combine_verdicts(["GO", "GO"], unreviewed_chunks=3), "INCOMPLETE")
        self.assertEqual(analysis.combine_verdicts([]), "UNKNOWN")

    def test_prompt_mentions_the_chunk_only_when_there_are_several(self):
        one = analysis.build_prompt("r", "b", {"id": "x", "type": "commit", "payload": "d"})
        self.assertNotIn("chunk", one.lower())
        two = analysis.build_prompt("r", "b", {"id": "x", "type": "commit", "payload": "d",
                                               "chunk_index": 2, "chunk_count": 3})
        self.assertIn("This is chunk 2 of 3", two)
        self.assertIn("VERDICT: GO or VERDICT: NO-GO", two)
        self.assertIn('"chunk": "2/3"', two)


@unittest.skipUnless(shutil.which("git"), "git not available")
class TestChunkedDiffReview(AIProfileTestBase):
    """analysis diff reviews every chunk of a large commit and combines the verdicts."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        def git(*args):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        (repo / "seed.txt").write_text("seed\n")
        git("add", "."); git("commit", "-q", "-m", "chore: seed")
        for i in range(4):
            (repo / f"mod{i}.py").write_text("".join(f"value_{i}_{n} = {n}\n" for n in range(60)))
        git("add", "."); git("commit", "-q", "-m", "feat: add modules")
        self.repo = repo

    def _run(self, replies, **budget):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            if req.full_url.endswith("/models"):
                return _Response(json.dumps({"data": [{"id": "m"}]}).encode())
            text = replies[min(len(calls) - 2, len(replies) - 1)]
            return _Response(json.dumps({"choices": [{"message": {"content": text}}]}).encode())

        args = argparse.Namespace(
            repo=str(self.repo), out_dir=str(Path(self.tmp.name) / "out"), base_ref="HEAD~1", max_commits=5,
            ai_base_url="http://ai-host:3000", ai_timeout=5, ai_max_diff_chars=budget.get("chars", 1000),
            ai_max_tokens=None, ai_max_chunks=budget.get("chunks"),
        )
        auth.set_ai_profile("http://ai-host:3000", "openai-chat")
        with mock.patch.object(analysis.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(analysis._run_diff_review(args), 0)
        report = json.loads((Path(args.out_dir) / "run_report.json").read_text())
        commit = [i for i in report["items"] if i["type"] == "commit"][0]
        return calls, commit

    def test_every_chunk_is_reviewed_and_verdicts_combine(self):
        calls, commit = self._run(["6) GO/NO-GO\n**GO**"])
        chunk_calls = [c for c in calls if c.full_url.endswith("/chat/completions")]
        self.assertGreater(commit["chunk_count"], 1)
        self.assertEqual(commit["chunks_reviewed"], commit["chunk_count"])
        self.assertEqual(commit["verdict"], "GO")
        self.assertNotIn("chunks", commit)  # full chunk text stays in the per-item JSON only
        bodies = [json.loads(c.data)["messages"][0]["content"] for c in chunk_calls]
        self.assertTrue(any(f"chunk 1 of {commit['chunk_count']}" in b for b in bodies))
        for i in range(4):  # every file reached the model
            self.assertTrue(any(f"mod{i}.py" in b for b in bodies), f"mod{i}.py never reviewed")
        md = Path(commit["output_md"]).read_text()
        self.assertIn("## Overall", md)
        self.assertIn(f"## Chunk {commit['chunk_count']} of {commit['chunk_count']}", md)

    def test_one_no_go_chunk_makes_the_item_no_go(self):
        replies = ["6) GO/NO-GO\n**GO**"] * 2 + ["6) GO/NO-GO\n**NO-GO**: removes a check"] + ["6) GO/NO-GO\n**GO**"] * 20
        _, commit = self._run(replies)
        self.assertEqual(commit["verdict"], "NO-GO")

    def test_chunk_cap_makes_the_item_incomplete_not_go(self):
        _, commit = self._run(["6) GO/NO-GO\n**GO**"], chunks=1)
        self.assertGreater(commit["chunk_count"], 1)
        self.assertEqual(commit["chunks_reviewed"], 1)
        self.assertEqual(commit["verdict"], "INCOMPLETE")


if __name__ == "__main__":
    unittest.main()
