"""Keys come from Hermes's profile secret scope and never travel over remote plain http."""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from cortiq_router.classify import classify_detailed
from cortiq_router.credentials import SECRETS, cleartext_to_remote, get_secret
from cortiq_router.execution import ExecutionError, call_endpoint, probe_models, resolve_api_key
from cortiq_router.providers import config_warnings


def _hermes_scope(secrets: dict, calls: list | None = None) -> dict:
    """Fake ``agent.secret_scope`` modules for ``patch.dict(sys.modules, ...)``."""
    def get_secret(name, default=None):
        if calls is not None:
            calls.append(name)
        return secrets.get(name, default)

    agent = types.ModuleType("agent")
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = get_secret
    agent.secret_scope = scope
    return {"agent": agent, "agent.secret_scope": scope}


class _RecordingOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        raise AssertionError("no request may be sent")


CLASSIFIER = {"apiKeyEnv": "CORTIQ_ROUTER_KEY", "routerUrl": "https://router.allaigate.com",
              "taxonomyId": "data-assistant", "timeoutMs": 15000, "maxChars": 12000,
              "routerProfile": "balanced"}


class SecretScopeTests(unittest.TestCase):
    def test_hermes_scope_wins_over_process_env(self):
        with patch.dict(sys.modules, _hermes_scope({"CORTIQ_ROUTER_KEY": "profile-b"})), \
                patch.dict("os.environ", {"CORTIQ_ROUTER_KEY": "process-env"}):
            self.assertEqual(get_secret("CORTIQ_ROUTER_KEY"), "profile-b")
            self.assertEqual(SECRETS.get("CORTIQ_ROUTER_KEY"), "profile-b")
            self.assertEqual(SECRETS["CORTIQ_ROUTER_KEY"], "profile-b")

    def test_scope_miss_is_not_filled_from_process_env(self):
        with patch.dict(sys.modules, _hermes_scope({})), \
                patch.dict("os.environ", {"OPENROUTER_API_KEY": "process-env"}):
            self.assertIsNone(get_secret("OPENROUTER_API_KEY"))
            self.assertEqual(SECRETS.get("OPENROUTER_API_KEY", "fallback"), "fallback")
            self.assertNotIn("OPENROUTER_API_KEY", SECRETS)
            with self.assertRaises(KeyError):
                SECRETS["OPENROUTER_API_KEY"]

    def test_outside_hermes_reads_process_env(self):
        with patch.dict(sys.modules, {"agent": None, "agent.secret_scope": None}), \
                patch.dict("os.environ", {"CORTIQ_DEMO_KEY": "from-env"}):
            self.assertEqual(get_secret("CORTIQ_DEMO_KEY"), "from-env")

    def test_tier_key_is_read_through_the_scope(self):
        entry = {"provider": "openai", "model": "gpt-x", "baseUrl": "https://api.openai.com/v1",
                 "apiKeyEnv": "OPENAI_API_KEY"}
        with patch.dict(sys.modules, _hermes_scope({"OPENAI_API_KEY": "profile-key"})), \
                patch.dict("os.environ", {"OPENAI_API_KEY": "process-env"}):
            self.assertEqual(resolve_api_key(entry), "profile-key")

    def test_classifier_key_is_read_through_the_scope(self):
        sent = []

        class _Opener:
            def open(self, request, timeout=None):
                sent.append(request.get_header("Authorization"))
                raise TimeoutError("stop here")

        with patch.dict(sys.modules, _hermes_scope({"CORTIQ_ROUTER_KEY": "profile-key"})), \
                patch.dict("os.environ", {"CORTIQ_ROUTER_KEY": "process-env"}):
            classify_detailed("hi", CLASSIFIER, log=lambda _msg: None, opener=_Opener())
        self.assertEqual(sent, ["Bearer profile-key"])

    def test_scope_error_degrades_classification(self):
        def refuse(name, default=None):
            raise RuntimeError("no profile scope")

        modules = _hermes_scope({})
        modules["agent.secret_scope"].get_secret = refuse
        opener = _RecordingOpener()
        logs = []
        with patch.dict(sys.modules, modules):
            decision, metadata = classify_detailed("hi", CLASSIFIER, log=logs.append, opener=opener)
        self.assertIsNone(decision)
        self.assertEqual(metadata["status"], "classifier_error")
        self.assertEqual(opener.requests, [])
        self.assertIn("lookup failed (RuntimeError)", logs[0])

    def test_config_warnings_use_the_scope(self):
        config = {"globalTiers": {"low": [{"provider": "openai", "model": "gpt-x"}]}}
        with patch.dict(sys.modules, _hermes_scope({})), \
                patch.dict("os.environ", {"OPENAI_API_KEY": "process-env"}):
            warnings = config_warnings(config)
        self.assertTrue(any("$OPENAI_API_KEY is missing" in warning for warning in warnings))


class CleartextRuleTests(unittest.TestCase):
    def test_rule(self):
        self.assertTrue(cleartext_to_remote("http://example.com/v1"))
        self.assertTrue(cleartext_to_remote("http://192.168.1.20:8080/v1"))
        self.assertFalse(cleartext_to_remote("https://example.com/v1"))
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:8080/v1", "http://[::1]:8080/v1"):
            self.assertFalse(cleartext_to_remote(url), url)

    def test_key_is_not_read_for_remote_plain_http(self):
        calls = []
        entry = {"provider": "custom", "model": "m", "baseUrl": "http://example.com/v1",
                 "apiKeyEnv": "CUSTOM_KEY"}
        with patch.dict(sys.modules, _hermes_scope({"CUSTOM_KEY": "secret"}, calls)):
            with self.assertRaises(ExecutionError) as caught:
                resolve_api_key(entry)
        self.assertEqual(caught.exception.reason, "insecure_url")
        self.assertEqual(calls, [])
        self.assertNotIn("secret", str(caught.exception) + caught.exception.detail)

    def test_loopback_http_still_gets_its_key(self):
        entry = {"provider": "custom", "model": "m", "baseUrl": "http://127.0.0.1:8080/v1",
                 "apiKeyEnv": "CUSTOM_KEY"}
        self.assertEqual(resolve_api_key(entry, {"CUSTOM_KEY": "local"}), "local")

    def test_keyless_remote_http_is_allowed(self):
        entry = {"provider": "custom", "model": "m", "baseUrl": "http://192.168.1.20:8080/v1"}
        self.assertIsNone(resolve_api_key(entry, {}))

    def test_direct_call_and_probe_refuse_before_any_request(self):
        entry = {"provider": "custom", "model": "m", "baseUrl": "http://example.com/v1",
                 "apiKeyEnv": "CUSTOM_KEY"}
        env = {"CUSTOM_KEY": "secret"}
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("network used")):
            with self.assertRaises(ExecutionError) as caught:
                call_endpoint(entry, {"messages": [{"role": "user", "content": "hi"}]}, env)
            self.assertEqual(caught.exception.reason, "insecure_url")
            self.assertEqual(probe_models(entry, env)["reason"], "insecure_url")

    def test_classifier_refuses_remote_plain_http(self):
        opener = _RecordingOpener()
        logs = []
        config = dict(CLASSIFIER, routerUrl="http://router.example.com")
        decision, metadata = classify_detailed("hi", config, env={"CORTIQ_ROUTER_KEY": "secret"},
                                               log=logs.append, opener=opener)
        self.assertIsNone(decision)
        self.assertEqual(metadata["status"], "classifier_error")
        self.assertEqual(opener.requests, [])
        self.assertIn("not sent", logs[0])
        self.assertNotIn("secret", logs[0])


if __name__ == "__main__":
    unittest.main()
