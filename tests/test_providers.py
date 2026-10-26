"""Provider-entry configuration and selection tests (no network access)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from cortiq_router.config import ConfigError, parse_config
from cortiq_router.providers import (
    config_warnings,
    entries_for_provider,
    iter_config_entries,
    normalize_entry,
)
from cortiq_router.select import candidates_for, select_model


DECISION = {
    "taskLabel": "coding",
    "complexityScore": 0.2,
    "complexityTier": "low",
}


def local_entry(**overrides):
    value = {
        "provider": "ollama",
        "model": "qwen3:8b",
        "timeoutMs": 2500,
        "extraBody": {"top_k": 20},
    }
    value.update(overrides)
    return value


class ProviderEntryTests(unittest.TestCase):
    def test_string_entry_remains_managed_openrouter(self):
        entry = normalize_entry("xiaomi/mimo-v2.6-flash")
        self.assertEqual(entry["provider"], "openrouter")
        self.assertEqual(entry["model"], "xiaomi/mimo-v2.6-flash")
        self.assertIsNone(entry["baseUrl"])
        self.assertFalse(entry["direct"])

    def test_presets_resolve_local_and_cloud_endpoints(self):
        expected = {
            "ollama": "http://127.0.0.1:11434/v1",
            "lmstudio": "http://127.0.0.1:1234/v1",
            "cortiq": "http://127.0.0.1:8080/v1",
            "openai": "https://api.openai.com/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "groq": "https://api.groq.com/openai/v1",
            "together": "https://api.together.xyz/v1",
        }
        for provider, base_url in expected.items():
            with self.subTest(provider=provider):
                entry = normalize_entry({"provider": provider, "model": "model-id"})
                self.assertEqual(entry["baseUrl"], base_url)
                self.assertTrue(entry["direct"])
        self.assertEqual(normalize_entry(local_entry())["apiMode"], "chat_completions")

    def test_explicit_vllm_url_and_anthropic_mode(self):
        vllm = normalize_entry({"provider": "vllm", "model": "qwen", "baseUrl": "http://127.0.0.1:9000/v1"})
        self.assertEqual(vllm["baseUrl"], "http://127.0.0.1:9000/v1")
        anthropic = normalize_entry({
            "provider": "anthropic", "model": "claude-haiku-4-5",
            "apiMode": "anthropic_messages",
        })
        self.assertEqual(anthropic["baseUrl"], "https://api.anthropic.com")
        self.assertEqual(anthropic["apiKeyEnv"], "ANTHROPIC_API_KEY")

    def test_explicit_native_provider_entry_has_no_direct_endpoint(self):
        config = parse_config({"globalTiers": {"medium": [{
            "provider": "openai-codex", "model": "gpt-5.6-terra", "native": True,
        }]}})
        entry = normalize_entry(config["globalTiers"]["medium"][0])
        self.assertTrue(entry["native"])
        self.assertFalse(entry["direct"])
        self.assertEqual(entry["routeKind"], "native")
        self.assertEqual(entry["provider"], "openai-codex")
        self.assertIsNone(entry["baseUrl"])

    def test_native_entry_rejects_direct_endpoint_fields(self):
        with self.assertRaises(ConfigError):
            parse_config({"globalTiers": {"low": [{
                "provider": "openai-codex", "model": "gpt-6-luna", "native": True,
                "baseUrl": "https://example.test/v1",
            }]}})

    def test_object_entries_are_valid_in_tiers_rules_and_default(self):
        config = parse_config({
            "globalTiers": {"low": [local_entry()]},
            "taskRules": {
                "coding": {"low": [local_entry(model="qwen3-coder:30b")], "any": local_entry(model="qwen3:14b")},
                "qa": [local_entry(model="qwen3:4b")],
            },
            "defaultModel": local_entry(model="qwen3:4b"),
        })
        self.assertEqual(config["globalTiers"]["low"][0]["model"], "qwen3:8b")
        paths = [path for path, _entry in iter_config_entries(config)]
        self.assertIn("globalTiers.low[0]", paths)
        self.assertIn("taskRules.coding.low[0]", paths)
        self.assertIn("taskRules.coding.any", paths)
        self.assertIn("defaultModel", paths)

    def test_invalid_entry_fields_are_rejected(self):
        bad_entries = [
            {"provider": "ollama"},
            {"model": "qwen"},
            local_entry(apiKey="must-not-be-here"),
            local_entry(apiMode="responses"),
            local_entry(baseUrl="127.0.0.1:11434/v1"),
            local_entry(baseUrl="https://user:pass@example.com/v1"),
            local_entry(apiKeyEnv="1BAD"),
            local_entry(timeoutMs=0),
            local_entry(extraBody=[]),
            {"provider": "vllm", "model": "qwen"},
            {"provider": "llamacpp", "model": "qwen"},
            {"provider": "custom", "model": "qwen"},
        ]
        for entry in bad_entries:
            with self.subTest(entry=entry), self.assertRaises(ConfigError):
                parse_config({"globalTiers": {"low": [entry]}})

    def test_local_model_id_does_not_require_openrouter_slash(self):
        config = parse_config({"globalTiers": {"low": [local_entry(model="qwen3:8b")]}})
        self.assertEqual(config["globalTiers"]["low"][0]["model"], "qwen3:8b")
        with self.assertRaises(ConfigError):
            parse_config({"globalTiers": {"low": [local_entry(model="bad model")]}})

    def test_extra_body_cannot_override_router_owned_fields(self):
        for reserved in ("model", "messages", "stream"):
            with self.subTest(reserved=reserved), self.assertRaises(ConfigError):
                parse_config({"globalTiers": {"low": [local_entry(extraBody={reserved: "x"})]}})

    def test_plain_http_with_a_key_requires_loopback(self):
        remote_key = {"provider": "custom", "model": "m", "baseUrl": "http://192.168.1.20:8000/v1",
                      "apiKeyEnv": "SOME_KEY"}
        with self.assertRaises(ConfigError):
            parse_config({"globalTiers": {"low": [remote_key]}})
        keyless = dict(remote_key)
        del keyless["apiKeyEnv"]
        parse_config({"globalTiers": {"low": [keyless]}})  # unauthenticated LAN server is allowed

    def test_missing_key_and_duplicates_are_warnings_not_secrets(self):
        raw = {"globalTiers": {"low": [local_entry(apiKeyEnv="MISSING_TEST_KEY"), local_entry(apiKeyEnv="MISSING_TEST_KEY")]}}
        config = parse_config(raw)
        with patch.dict("os.environ", {}, clear=True):
            warnings = config_warnings(config)
        self.assertTrue(any("MISSING_TEST_KEY" in warning and "missing" in warning for warning in warnings))
        self.assertTrue(any("duplicate" in warning for warning in warnings))
        self.assertNotIn("must-not-be-here", "\n".join(warnings))


class ProviderSelectionTests(unittest.TestCase):
    def test_selection_preserves_model_id_and_exposes_normalized_entry(self):
        config = {
            "globalTiers": {"low": [local_entry()]},
            "taskRules": {},
            "defaultModel": "fallback/model",
            "complexityBands": None,
        }
        selection = select_model(config, DECISION)
        self.assertEqual(selection["model"], "qwen3:8b")
        self.assertEqual(selection["candidates"], ["qwen3:8b", "fallback/model"])
        self.assertEqual(selection["entry"]["provider"], "ollama")
        self.assertTrue(selection["entry"]["direct"])
        self.assertEqual(selection["entries"][0]["model"], "qwen3:8b")

    def test_task_rule_object_and_priority_work_with_provider_entries(self):
        config = {
            "globalTiers": {"low": [local_entry(model="global")]},
            "taskRules": {"coding": {"low": [local_entry(model="task")]}},
            "defaultModel": local_entry(model="default"),
            "complexityBands": None,
        }
        selection = select_model(config, DECISION)
        self.assertEqual(selection["model"], "task")
        self.assertTrue(selection["taskRuleMatched"])
        self.assertEqual(selection["candidates"], ["task", "global", "default"])
        self.assertEqual(candidates_for(config, "coding", "low"), (["task", "global", "default"], True))

    def test_same_model_on_different_endpoints_is_not_deduplicated(self):
        other = local_entry(provider="custom", baseUrl="http://127.0.0.1:9999/v1")
        config = {"globalTiers": {"low": [local_entry(), other]}, "taskRules": {}, "defaultModel": ""}
        selection = select_model(config, DECISION)
        self.assertEqual(len(selection["entries"]), 2)
        self.assertEqual(selection["candidates"], ["qwen3:8b", "qwen3:8b"])

    def test_classifier_fallback_supports_object_default(self):
        config = {"globalTiers": {}, "taskRules": {}, "defaultModel": local_entry()}
        selection = select_model(config, None)
        self.assertEqual(selection["model"], "qwen3:8b")
        self.assertEqual(selection["entry"]["provider"], "ollama")
        self.assertTrue(selection["fallback"])

    def test_provider_lookup_uses_first_matching_entry_in_config_order(self):
        config = parse_config({
            "globalTiers": {"low": [local_entry(model="first")], "high": [local_entry(model="second")]},
            "defaultModel": "fallback/model",
        })
        self.assertEqual(entries_for_provider(config, "ollama")[0]["model"], "first")
        self.assertEqual(entries_for_provider(config, "openrouter")[0]["model"], "fallback/model")


if __name__ == "__main__":
    unittest.main()
