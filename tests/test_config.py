"""Unit tests for config validation — the family defaults table is the source of truth."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from cortiq_router.select import select_model
from cortiq_router.config import (
    DEFAULTS,
    ConfigError,
    default_config_path,
    load_config,
    parse_config,
)


class DefaultsTests(unittest.TestCase):
    def test_defaults_match_family_table(self):
        self.assertEqual(DEFAULTS, {
            "enabled": True,
            "apiKeyEnv": "CORTIQ_ROUTER_KEY",
            "routerUrl": "https://router.allaigate.com",
            "taxonomyId": "data-assistant",
            "timeoutMs": 15000,
            "maxChars": 12000,
            "routerProfile": "balanced",
            "complexityBands": None,
            "globalTiers": {},
            "taskRules": {},
            "defaultModel": "",
            "echoRouting": False,
        })

    def test_empty_object_equals_defaults(self):
        self.assertEqual(parse_config({}), DEFAULTS)


class ValidationTests(unittest.TestCase):
    def test_unknown_setting_is_an_error(self):
        with self.assertRaisesRegex(ConfigError, "unknown setting"):
            parse_config({"typoField": 1})

    def test_root_must_be_object(self):
        with self.assertRaises(ConfigError):
            parse_config(["not", "an", "object"])

    def test_timeout_bounds(self):
        for bad in (0, 120001, -5, 1.5, "15000"):
            with self.assertRaises(ConfigError):
                parse_config({"timeoutMs": bad})
        self.assertEqual(parse_config({"timeoutMs": 15000})["timeoutMs"], 15000)

    def test_maxchars_bounds(self):
        for bad in (0, 1000001, True):
            with self.assertRaises(ConfigError):
                parse_config({"maxChars": bad})
        self.assertEqual(parse_config({"maxChars": 1})["maxChars"], 1)

    def test_router_profile_enum(self):
        self.assertEqual(parse_config({"routerProfile": "cost-saver"})["routerProfile"], "cost-saver")
        with self.assertRaises(ConfigError):
            parse_config({"routerProfile": "fastest"})

    def test_complexity_bands_validation(self):
        good = parse_config({"complexityBands": {"low": 0.35, "medium": 0.65}})
        self.assertEqual(good["complexityBands"], {"low": 0.35, "medium": 0.65})
        for bad in ({"low": 0.5, "medium": 0.5}, {"low": -0.1, "medium": 0.5},
                    {"low": 0.2, "medium": 1.5}, {"low": 0.3}, {"extra": 1, "low": 0.1, "medium": 0.2}):
            with self.assertRaises(ConfigError):
                parse_config({"complexityBands": bad})

    def test_model_id_format(self):
        self.assertEqual(parse_config({"defaultModel": "deepseek/deepseek-chat"})["defaultModel"],
                         "deepseek/deepseek-chat")
        self.assertEqual(parse_config({"defaultModel": "gpt-6-luna"})["defaultModel"], "gpt-6-luna")
        for bad in ("a/b c", "   ", 42):
            with self.assertRaises(ConfigError):
                parse_config({"defaultModel": bad})

    def test_router_url_rules(self):
        self.assertEqual(parse_config({"routerUrl": "https://router.allaigate.com/"})["routerUrl"],
                         "https://router.allaigate.com")
        self.assertEqual(parse_config({"routerUrl": "http://127.0.0.1:9099"})["routerUrl"],
                         "http://127.0.0.1:9099")
        with self.assertRaises(ConfigError):
            parse_config({"routerUrl": "http://example.com"})
        with self.assertRaises(ConfigError):
            parse_config({"routerUrl": "ftp://router.allaigate.com"})
        with self.assertRaises(ConfigError):
            parse_config({"routerUrl": "https://user:pass@router.allaigate.com"})

    def test_codex_native_tiers_with_direct_openrouter_high_parse(self):
        config = parse_config({
            "globalTiers": {
                "low": ["gpt-6-luna"], "medium": ["gpt-5.6-terra"],
                "high": [{"provider": "openrouter", "model": "moonshotai/kimi-k3.1-pro"}],
            },
            "taskRules": {}, "defaultModel": "gpt-6-luna",
        })
        high = select_model(config, {"taskLabel": "coding", "complexityScore": 0.9,
                                     "complexityTier": "high"})["entry"]
        self.assertTrue(high["direct"])
        self.assertEqual(high["provider"], "openrouter")
        self.assertEqual(high["apiKeyEnv"], "OPENROUTER_API_KEY")
        self.assertEqual(high["model"], "moonshotai/kimi-k3.1-pro")

    def test_global_tiers_shape(self):
        cfg = parse_config({"globalTiers": {"low": ["a/b"], "high": ["x/y"]}})
        self.assertEqual(cfg["globalTiers"]["high"], ["x/y"])
        # family rule: globalTiers values must be arrays (single strings are for taskRules only)
        with self.assertRaises(ConfigError):
            parse_config({"globalTiers": {"high": "x/y"}})
        with self.assertRaises(ConfigError):
            parse_config({"globalTiers": {"ultra": ["a/b"]}})
        with self.assertRaises(ConfigError):
            parse_config({"globalTiers": {"low": ["bad model"]}})
        # ChatGPT/Codex model ids are bare, unlike OpenRouter's provider/model ids.
        cfg = parse_config({"globalTiers": {"low": ["gpt-6-luna"],
                                              "medium": ["gpt-5.6-terra"]},
                            "defaultModel": "gpt-6-luna"})
        self.assertEqual(cfg["globalTiers"]["low"], ["gpt-6-luna"])
        self.assertEqual(cfg["globalTiers"]["medium"], ["gpt-5.6-terra"])
        self.assertEqual(cfg["defaultModel"], "gpt-6-luna")

    def test_task_rules_shape(self):
        cfg = parse_config({"taskRules": {
            "coding": {"low": "a/b", "high": ["c/d"], "any": ["e/f"]},
            "chitchat": "g/h",
        }})
        self.assertEqual(cfg["taskRules"]["chitchat"], "g/h")
        self.assertEqual(parse_config({"taskRules": {"coding": {"low": ["gpt-6-luna"]}}})
                         ["taskRules"]["coding"]["low"], ["gpt-6-luna"])
        with self.assertRaises(ConfigError):
            parse_config({"taskRules": {"coding": {"wrong": ["a/b"]}}})
        with self.assertRaises(ConfigError):
            parse_config({"taskRules": {"coding": {"low": ["bad model"]}}})

    def test_boolean_fields_must_be_booleans(self):
        with self.assertRaises(ConfigError):
            parse_config({"enabled": "yes"})
        with self.assertRaises(ConfigError):
            parse_config({"echoRouting": 1})

    def test_api_key_env_identifier(self):
        self.assertEqual(parse_config({"apiKeyEnv": "MY_KEY"})["apiKeyEnv"], "MY_KEY")
        for bad in ("1BAD", "with-dash", "with space", ""):
            with self.assertRaises(ConfigError):
                parse_config({"apiKeyEnv": bad})


class LoadConfigTests(unittest.TestCase):
    def test_missing_default_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp}
            old = os.environ.get("HOME")
            os.environ["HOME"] = tmp
            try:
                self.assertEqual(load_config(env=env), parse_config({}))
            finally:
                if old is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old

    def test_missing_explicit_file_is_an_error(self):
        with self.assertRaisesRegex(ConfigError, "Cannot read"):
            load_config(path="/nonexistent/cortiq.json", env={})

    def test_missing_env_selected_file_is_an_error(self):
        with self.assertRaisesRegex(ConfigError, "Cannot read"):
            load_config(env={"CORTIQ_ROUTER_CONFIG": "/nonexistent/cortiq.json"})

    def test_invalid_json_is_an_error(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{not json")
            path = handle.name
        try:
            with self.assertRaisesRegex(ConfigError, "valid JSON"):
                load_config(path=path, env={})
        finally:
            os.unlink(path)

    def test_reads_and_validates_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"defaultModel": "deepseek/deepseek-v4-flash", "routerProfile": "quality-first"},
                      handle)
            path = handle.name
        try:
            cfg = load_config(path=path, env={})
            self.assertEqual(cfg["defaultModel"], "deepseek/deepseek-v4-flash")
            self.assertEqual(cfg["routerProfile"], "quality-first")
            self.assertEqual(cfg["timeoutMs"], 15000)
        finally:
            os.unlink(path)

    def test_env_var_selects_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "my.json"
            target.write_text("{}", encoding="utf-8")
            env = {"CORTIQ_ROUTER_CONFIG": str(target)}
            self.assertEqual(default_config_path(env), target)
            self.assertEqual(load_config(env=env), parse_config({}))


class ExampleConfigTests(unittest.TestCase):
    """cortiq-router.example.json is the shipped, documented example."""

    def _raw(self):
        import json as _json
        from pathlib import Path as _Path
        path = _Path(__file__).resolve().parent.parent / "cortiq-router.example.json"
        return _json.loads(path.read_text(encoding="utf-8"))

    def test_example_parses_with_owner_tiers(self):
        config = parse_config(self._raw())
        self.assertEqual(config["globalTiers"], {
            "low": ["xiaomi/mimo-v2.6-flash"],
            "medium": ["anthropic/claude-haiku-5.5"],
            "high": ["moonshotai/kimi-k3.1-pro"],
        })
        self.assertEqual(config["defaultModel"], "xiaomi/mimo-v2.6-flash")
        self.assertEqual(config["apiKeyEnv"], "CORTIQ_ROUTER_KEY")
        self.assertEqual(config["routerUrl"], "https://router.allaigate.com")
        self.assertEqual(config["taxonomyId"], "data-assistant")
        self.assertEqual(config["timeoutMs"], 15000)
        self.assertEqual(config["maxChars"], 12000)
        self.assertEqual(config["routerProfile"], "balanced")
        # complexityBands is an OPTION; the example ships the default (router tier)
        self.assertIsNone(config["complexityBands"])
        # ...but the option itself stays valid
        self.assertEqual(parse_config({"complexityBands": {"low": 0.35, "medium": 0.65}})["complexityBands"],
                         {"low": 0.35, "medium": 0.65})
        self.assertEqual(config["echoRouting"], False)
        self.assertEqual(config["enabled"], True)
        for tier_models in config["globalTiers"].values():
            for model in tier_models:
                self.assertRegex(model, r"^[^/\s]+/\S+$")

    def test_example_selects_expected_tier_models(self):
        config = parse_config(self._raw())
        for score, tier, model in [
            (0.12, "low", "xiaomi/mimo-v2.6-flash"),
            (0.50, "medium", "anthropic/claude-haiku-5.5"),
            (0.85, "high", "moonshotai/kimi-k3.1-pro"),
        ]:
            decision = {"taskLabel": "chitchat", "complexityScore": score,
                        "complexityTier": tier, "confidence": 0.9,
                        "requestId": "req_t", "isNovel": False, "source": "router"}
            selection = select_model(config, decision)
            self.assertEqual(selection["tier"], tier)
            self.assertEqual(selection["model"], model)


if __name__ == "__main__":
    unittest.main()