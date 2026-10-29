"""Plugin-level tests: routing/cost-control middleware, fail-open execution, slash commands.

Every endpoint here is the local mock server; no network and no real provider key.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tests.mock_server import MOCK_KEY, MockServer

DECISION = {
    "taskLabel": "coding",
    "complexityScore": 0.9,
    "complexityTier": "high",
}


def _load_plugin_module():
    import importlib.util
    import sys

    name = "cortiq_hermes_router_plugin"
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeContext:
    def __init__(self, capabilities=()):
        self.state = FakeState()
        self.capabilities = set(capabilities)
        self.commands = {}
        self.config = {}
        self.middlewares = {}
        self.llm: Any = None

    def register_middleware(self, kind, callback, **_kwargs):
        self.middlewares.setdefault(kind, []).append(callback)

    def register_command(self, name, handler=None, **kwargs):
        self.commands[name] = {"handler": handler, **kwargs}

    def has_capability(self, name):
        return name in self.capabilities

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value


class FakeNativeLlm:
    def __init__(self):
        self.calls = []

    def complete(self, messages, **kwargs):
        from types import SimpleNamespace
        self.calls.append({"messages": messages, **kwargs})
        return SimpleNamespace(
            text="pong from native", provider=kwargs["provider"], model=kwargs["model"],
            usage=SimpleNamespace(input_tokens=7, output_tokens=3, total_tokens=10),
        )


class PluginTestCase(unittest.TestCase):
    """Shared fixture: mock endpoint, temp config, both capabilities granted."""

    def setUp(self):
        self.plugin = _load_plugin_module()
        self.server = MockServer().__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.__exit__, None, None, None)
        self.config_path = Path(self.tmp.name) / "config.json"
        self.write_config({
            "enabled": True,
            "globalTiers": {"low": ["cheap/mini"], "high": ["strong/big"]},
            "taskRules": {},
            "defaultModel": "fallback/model",
        })
        self.ctx = FakeContext(capabilities={"llm.model_override", "llm.provider_override"})
        self.registered = self.plugin.register(self.ctx)
        self.handler = self.ctx.commands["cortiq-router"]["handler"]
        self.classify_calls = []
        patcher = patch.object(
            self.plugin, "classify_detailed",
            side_effect=self._classify,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _classify(self, text, config):
        self.classify_calls.append(text)
        if getattr(self, "fail_classification", False):
            return None, {"status": "classifier_error", "classifier_latency_ms": 1.0}
        return DECISION, {"status": "ok", "classifier_latency_ms": 2.0}

    def write_config(self, config):
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        os.environ["CORTIQ_ROUTER_CONFIG"] = str(self.config_path)
        self.addCleanup(os.environ.pop, "CORTIQ_ROUTER_CONFIG", None)

    def request(self, model="hermes/model", messages=None, **context):
        payload = {"model": model, "messages": messages or [{"role": "user", "content": "hello"}]}
        context.setdefault("session_id", "s1")
        context.setdefault("api_request_id", "req-1")
        result = self.ctx.middlewares["llm_request"][0](payload, **context)
        return (result or {}).get("request", payload)

    def snapshot(self):
        return self.ctx.state.get("statistics") or {}


class CostControlTests(PluginTestCase):
    """Item A: one classified decision per turn, cheap model for tool-loop steps."""

    def test_first_call_is_classified_and_uses_the_expensive_tier(self):
        sent = self.request(api_call_count=1, turn_id="t1")
        self.assertEqual(sent["model"], "strong/big")
        self.assertEqual(len(self.classify_calls), 1)
        stats = self.snapshot()["totals"]
        self.assertEqual(stats["by_status"]["ok"], 1)
        self.assertEqual(stats["step_continuation_count"], 0)

    def test_tool_loop_steps_skip_classification_and_use_the_low_tier(self):
        self.request(api_call_count=1, turn_id="t1")
        for step in (2, 3, 4):
            sent = self.request(api_call_count=step, turn_id="t1")
            self.assertEqual(sent["model"], "cheap/mini")
        self.assertEqual(len(self.classify_calls), 1, "classifier must not run per tool step")
        stats = self.snapshot()["totals"]
        self.assertEqual(stats["calls"], 4)
        self.assertEqual(stats["step_continuation_count"], 3)
        self.assertEqual(stats["by_status"]["step_continuation"], 3)
        self.assertEqual(stats["by_status"]["ok"], 1)
        self.assertAlmostEqual(stats.get("avg_score"), DECISION["complexityScore"])

    def test_continuation_falls_back_to_default_model_when_low_tier_is_empty(self):
        self.write_config({"enabled": True, "globalTiers": {}, "taskRules": {},
                           "defaultModel": "fallback/model"})
        sent = self.request(api_call_count=7, turn_id="t2")
        self.assertEqual(sent["model"], "fallback/model")
        recent = self.snapshot()["recent"][-1]
        self.assertTrue(recent["step_continuation"])

    def test_tier_reasoning_sets_none_for_low_and_high_for_medium(self):
        self.write_config({
            "enabled": True,
            "globalTiers": {"low": ["gpt-5.6-terra"], "medium": ["gpt-5.6-terra"],
                            "high": ["strong/big"]},
            "tierReasoning": {"low": "none", "medium": "high"},
            "taskRules": {}, "defaultModel": "gpt-5.6-terra",
            "complexityBands": {"low": 0.45, "medium": 0.6},
        })
        low = self.request(api_call_count=2, turn_id="t-low")
        self.assertEqual(low["reasoning"], {"enabled": False})
        with patch.object(self.plugin, "classify_detailed", return_value=(
            {"taskLabel": "qa", "complexityScore": 0.5, "complexityTier": "medium"},
            {"status": "ok", "classifier_latency_ms": 1.0},
        )):
            medium = self.request(api_call_count=1, turn_id="t-medium")
        self.assertEqual(medium["reasoning"], {"enabled": True, "effort": "high"})

    def test_new_turn_id_reclassifies(self):
        self.request(api_call_count=1, turn_id="t1")
        sent = self.request(api_call_count=1, turn_id="t2")
        self.assertEqual(len(self.classify_calls), 2)
        self.assertEqual(sent["model"], "strong/big")

    def test_same_turn_first_call_uses_the_cached_decision(self):
        self.request(api_call_count=1, turn_id="t1")
        sent = self.request(api_call_count=1, turn_id="t1")
        self.assertEqual(len(self.classify_calls), 1)
        self.assertEqual(sent["model"], "strong/big")

    def test_continuation_to_a_direct_endpoint_runs_directly_and_fails_open(self):
        self.write_config({
            "enabled": True,
            "globalTiers": {"low": [{"provider": "ollama", "model": "plain-json",
                                     "baseUrl": self.server.base_url}]},
            "taskRules": {},
            "defaultModel": "fallback/model",
        })
        sent = self.request(api_call_count=2, turn_id="t3")
        self.assertEqual(sent["model"], "plain-json", "tool step uses the cheap direct endpoint")
        execution = self.ctx.middlewares["llm_execution"][0]
        result = execution(sent, lambda payload: "terminal", api_request_id="req-1")
        self.assertEqual(result.choices[0].message.content, "pong from plain json")
        self.assertEqual(sum(1 for item in self.server.seen if item["method"] == "POST"), 1)
        self.assertEqual(len(self.classify_calls), 0, "tool steps never classify")

    def test_missing_api_call_count_behaves_like_the_first_call(self):
        payload = {"model": "hermes/model", "messages": [{"role": "user", "content": "hi"}]}
        result = self.ctx.middlewares["llm_request"][0](
            payload, api_request_id="req-x", session_id="s1")
        self.assertEqual(result["request"]["model"], "strong/big")
        self.assertEqual(len(self.classify_calls), 1)


class DirectExecutionTests(PluginTestCase):
    def _direct_config(self, model="qwen3:8b", **entry_extra):
        entry = {"provider": "ollama", "model": model, "baseUrl": self.server.base_url}
        entry.update(entry_extra)
        # DECISION classifies as "high", so the high chain must carry the endpoint;
        # low keeps a different model so tier selection is observable.
        self.write_config({"enabled": True,
                           "globalTiers": {"low": [dict(entry, model="cheap/model")],
                                           "high": [entry]},
                           "taskRules": {}, "defaultModel": "fallback/model"})

    def _execute(self, request, request_id="req-1", next_payload=None):
        terminal = []
        result = self.ctx.middlewares["llm_execution"][0](
            request,
            lambda payload: terminal.append(payload) or "from-hermes",
            api_request_id=request_id,
        )
        return result, terminal

    def test_explicit_native_provider_uses_hermes_llm_and_adapts_codex_response(self):
        native = FakeNativeLlm()
        self.ctx.llm = native
        self.write_config({
            "enabled": True,
            "globalTiers": {"low": ["gpt-6-luna"], "high": [{
                "provider": "openai-codex", "model": "gpt-5.6-terra", "native": True,
            }]},
            "taskRules": {}, "defaultModel": "gpt-6-luna",
        })
        codex = {"model": "gpt-6-luna", "instructions": "be brief", "input": [
            {"type": "message", "role": "user", "content": "use the configured native provider"}],
        }
        result = self.ctx.middlewares["llm_request"][0](
            codex, api_request_id="native-1", session_id="s1", turn_id="t-native",
            api_call_count=1, provider="openai-codex", api_mode="codex_responses",
        )
        reply, terminal = self._execute(result["request"], request_id="native-1")
        self.assertEqual(terminal, [])
        self.assertEqual(reply.status, "completed")
        self.assertEqual(reply.output[0].content[0].text, "pong from native")
        self.assertEqual(native.calls[0]["provider"], "openai-codex")
        self.assertEqual(native.calls[0]["model"], "gpt-5.6-terra")
        self.assertEqual(native.calls[0]["messages"][0]["content"], "be brief")
        recent = self.snapshot()["recent"][-1]
        self.assertEqual(recent["provider"], "openai-codex")
        self.assertEqual(recent["route_kind"], "native")

    def test_native_tool_request_falls_open_with_stable_reason(self):
        self.ctx.llm = FakeNativeLlm()
        self.write_config({
            "enabled": True,
            "globalTiers": {"high": [{"provider": "openai-codex", "model": "gpt-5.6-terra", "native": True}]},
            "taskRules": {}, "defaultModel": "gpt-6-luna",
        })
        codex = {"model": "gpt-6-luna", "input": [{
            "type": "message", "role": "user", "content": "call a tool"}],
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]}
        result = self.ctx.middlewares["llm_request"][0](
            codex, api_request_id="native-tools", session_id="s1", turn_id="t-native-tools",
            api_call_count=1, provider="openai-codex", api_mode="codex_responses",
        )
        reply, terminal = self._execute(result["request"], request_id="native-tools")
        self.assertEqual(reply, "from-hermes")
        self.assertEqual(terminal[0]["model"], "gpt-6-luna")
        self.assertEqual(self.snapshot()["totals"]["fallback_reasons"]["native_tools_unsupported"], 1)
        self.assertEqual(self.ctx.llm.calls, [])

    def test_codex_responses_high_direct_openrouter_full_path(self):
        os.environ["OPENROUTER_API_KEY"] = MOCK_KEY
        self.addCleanup(os.environ.pop, "OPENROUTER_API_KEY", None)
        self.write_config({
            "enabled": True,
            "globalTiers": {
                "low": ["gpt-6-luna"],
                "high": [{"provider": "openrouter", "model": "plain-json", "baseUrl": self.server.base_url}],
            },
            "taskRules": {}, "defaultModel": "gpt-6-luna",
        })
        codex = {
            "model": "gpt-6-luna", "instructions": "be brief",
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "summarize this"}]}],
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
        }
        result = self.ctx.middlewares["llm_request"][0](
            codex, api_request_id="codex-1", session_id="s1", turn_id="t-codex",
            api_call_count=1, provider="openai-codex", api_mode="codex_responses",
        )
        sent = result["request"]
        self.assertEqual(sent["model"], "plain-json")
        reply, terminal = self._execute(sent, request_id="codex-1")
        self.assertEqual(terminal, [])
        self.assertEqual(reply.status, "completed")
        self.assertEqual(reply.output[0].content[0].text, "pong from plain json")
        upstream = self.server.seen[-1]["body"]
        self.assertEqual(upstream["model"], "plain-json")
        self.assertIn("messages", upstream)
        for incompatible in ("instructions", "input", "reasoning", "max_output_tokens", "prompt_cache_key"):
            self.assertNotIn(incompatible, upstream)
        recent = self.snapshot()["recent"][-1]
        self.assertEqual(recent["provider"], "openrouter")
        self.assertEqual(recent["route_kind"], "direct")

    def test_direct_call_happy_path_returns_the_endpoint_reply(self):
        self._direct_config()
        sent = self.request(api_call_count=1, turn_id="t1")
        self.assertEqual(sent["model"], "qwen3:8b")
        result, terminal = self._execute(sent)
        self.assertEqual(result.choices[0].message.content, "pong from local")
        self.assertEqual(terminal, [], "terminal call must not run after a successful direct call")
        self.assertEqual(self.snapshot()["totals"].get("execution_fallback_count", 0), 0)

    def test_http_500_falls_open_and_records_the_reason(self):
        self._direct_config(model="http-500")
        sent = self.request(api_call_count=1, turn_id="t1")
        result, terminal = self._execute(sent)
        self.assertEqual(result, "from-hermes")
        self.assertEqual(terminal[0]["model"], "hermes/model",
                         "fallback restores the model Hermes was going to use")
        totals = self.snapshot()["totals"]
        self.assertEqual(totals["execution_fallback_count"], 1)
        self.assertEqual(totals["fallback_reasons"]["http_500"], 1)
        self.assertEqual(totals["fallback_by_provider"]["ollama"], 1)
        recent = self.snapshot()["recent"][-1]
        self.assertEqual(recent["status"], "execution_fallback")
        self.assertEqual(recent["route_kind"], "fallback")
        self.assertEqual(recent["model"], "hermes/model")

    def test_timeout_falls_open(self):
        self._direct_config(model="slow", timeoutMs=300)
        sent = self.request(api_call_count=1, turn_id="t1")
        result, terminal = self._execute(sent)
        self.assertEqual(result, "from-hermes")
        self.assertEqual(self.snapshot()["totals"]["fallback_reasons"]["timeout"], 1)

    def test_broken_json_falls_open(self):
        self._direct_config(model="bad-json")
        sent = self.request(api_call_count=1, turn_id="t1")
        self._execute(sent)
        self.assertEqual(self.snapshot()["totals"]["fallback_reasons"]["invalid_json"], 1)

    def test_missing_key_env_falls_open_without_leaking_the_value(self):
        self._direct_config(apiKeyEnv="MISSING_RUN_KEY")
        sent = self.request(api_call_count=1, turn_id="t1")
        result, _terminal = self._execute(sent)
        self.assertEqual(result, "from-hermes")
        totals = self.snapshot()["totals"]
        self.assertEqual(totals["fallback_reasons"]["missing_key"], 1)
        self.assertNotIn("MISSING_RUN_KEY", json.dumps(self.snapshot()))
        self.assertNotIn(MOCK_KEY, json.dumps(self.snapshot()))

    def test_ungranted_provider_capability_falls_open_without_trying_the_endpoint(self):
        self.ctx.capabilities = {"llm.model_override"}  # provider_override withdrawn
        self.ctx.middlewares.clear()  # re-register: fresh caches, single callback
        self.ctx.config.clear()
        # Re-register the SAME module setUp patched: register() always builds a fresh
        # cache (capability/config lookups re-run), while a second `_load_plugin_module()`
        # would drop the classify_detailed patch and send a real network request — the
        # test then depends on CORTIQ_ROUTER_KEY being present, which is exactly how it
        # failed in CI.
        self.plugin.register(self.ctx)
        self._direct_config()
        sent = self.request(api_call_count=1, turn_id="t1")
        result, _terminal = self._execute(sent)
        self.assertEqual(result, "from-hermes")
        stats = self.snapshot()["totals"]
        self.assertEqual(stats["by_status"].get("provider_override_not_granted", 0), 1)
        # Nothing was ever scheduled for a direct call, so there is no execution fallback.
        self.assertEqual(stats.get("execution_fallback_count", 0), 0)
        self.assertNotIn("provider_override_not_granted", stats.get("fallback_reasons", {}))
        self.assertEqual(len(self.server.seen), 0, "no request may leave the host without the grant")

    def test_string_entries_never_enter_the_execution_path(self):
        self.write_config({"enabled": True,
                           "globalTiers": {"low": ["cheap/mini"], "high": ["strong/big"]},
                           "taskRules": {}, "defaultModel": "fallback/model"})
        sent = self.request(api_call_count=1, turn_id="t1")
        self.assertEqual(sent["model"], "strong/big", "string entries stay on Hermes' client")
        result, terminal = self._execute(sent)
        self.assertEqual(result, "from-hermes")
        self.assertEqual(len(self.server.seen), 0)
        self.assertEqual(self.snapshot()["totals"]["execution_fallback_count"], 0)


class SlashCommandTests(PluginTestCase):
    def _direct_config(self):
        self.write_config({
            "enabled": True,
            "globalTiers": {"low": [
                {"provider": "ollama", "model": "qwen3:8b", "apiKeyEnv": "CORTIQ_DEMO_KEY",
                 "baseUrl": self.server.base_url}]},
            "taskRules": {},
            "defaultModel": "fallback/model",
        })

    def test_providers_probes_models_and_never_shows_the_key(self):
        os.environ["CORTIQ_DEMO_KEY"] = MOCK_KEY
        self.addCleanup(os.environ.pop, "CORTIQ_DEMO_KEY", None)
        self._direct_config()
        text = self.handler("providers")
        self.assertIn("GET {baseUrl}/models", text)
        self.assertIn(self.server.base_url.replace("/v1", "") + "/v1", text)
        self.assertIn("CORTIQ_DEMO_KEY set", text)
        self.assertIn("model listed", text)
        self.assertNotIn(MOCK_KEY, text)
        self.assertTrue(all(item["method"] == "GET" for item in self.server.seen))

    def test_providers_without_direct_endpoints_says_so(self):
        text = self.handler("providers")
        self.assertIn("no direct endpoints", text)

    def test_providers_reports_a_dead_endpoint_without_failing(self):
        self.write_config({"enabled": True,
                           "globalTiers": {"low": [
                               {"provider": "ollama", "model": "qwen3:8b",
                                "baseUrl": "http://127.0.0.1:9/v1"}]},
                           "taskRules": {}, "defaultModel": "fallback/model"})
        text = self.handler("providers")
        self.assertIn("failed: connection", text)

    def test_test_command_runs_one_explicit_completion(self):
        self.write_config({"enabled": True,
                           "globalTiers": {"low": [
                               {"provider": "ollama", "model": "qwen3:8b",
                                "baseUrl": self.server.base_url}]},
                           "taskRules": {}, "defaultModel": "fallback/model"})
        text = self.handler("test low")
        self.assertIn("test — tier low", text)
        self.assertIn("result     ok", text)
        completions = [item for item in self.server.seen if item["method"] == "POST"]
        self.assertEqual(len(completions), 1)
        self.assertEqual(completions[0]["body"]["max_tokens"], 8)
        self.assertIn("runs only on this explicit command", text)

    def test_by_provider_and_fallback_breakdown_after_a_real_direct_call(self):
        self.write_config({"enabled": True,
                           "globalTiers": {"high": [{"provider": "ollama", "model": "qwen3:8b",
                                                     "baseUrl": self.server.base_url}]},
                           "taskRules": {}, "defaultModel": "fallback/model"})
        sent = self.request(api_call_count=1, turn_id="t-bp")
        self.assertEqual(sent["model"], "qwen3:8b")
        self.ctx.middlewares["llm_execution"][0](sent, lambda payload: "hermes", api_request_id="req-1")
        totals = json.loads(self.handler("stats --json"))["totals"]
        self.assertEqual(totals["by_provider"], {"ollama": 1})
        text = self.handler("stats all")
        self.assertIn("By provider", text)

    def test_test_never_calls_a_managed_string_tier(self):
        self.write_config({"enabled": True, "globalTiers": {"low": ["cheap/mini"]},
                           "taskRules": {}, "defaultModel": "fallback/model"})
        text = self.handler("test low")
        self.assertIn("Hermes-managed string entries", text)
        self.assertEqual(self.server.seen, [])

    def test_test_requires_exactly_one_target(self):
        self.assertEqual(self.handler("test"), "usage: /cortiq-router test <low|medium|high|provider-name>")
        self.assertEqual(self.handler("test nope"), "cortiq-hermes-router test: no entries configured for provider nope")

    def test_status_shows_both_capabilities_and_endpoints(self):
        self._direct_config()
        text = self.handler("status")
        self.assertIn("llm.model_override: granted", text)
        self.assertIn("llm.provider_override: granted", text)
        self.assertIn("endpoints      ollama", text)
        self.assertNotIn(MOCK_KEY, text)

    def test_help_mentions_providers_and_test(self):
        text = self.handler("help")
        self.assertIn("/cortiq-router providers", text)
        self.assertIn("/cortiq-router test <tier|provider>", text)

    def test_stats_json_exposes_provider_breakdown(self):
        self.ctx.state.set("statistics", {
            "version": 1, "timezone": "UTC", "daily": {}, "recent": [],
            "totals": {"calls": 3, "by_tier": {"high": 3}, "by_task": {}, "by_model": {},
                       "by_status": {}, "fallback_count": 0, "avg_score": 0.5, "score_count": 3,
                       "avg_classifier_latency_ms": 2.0, "classifier_latency_count": 3,
                       "max_classifier_latency_ms": 2.0},
        })
        self.ctx.middlewares.clear()  # re-register so the aggregator loads the seeded state
        _load_plugin_module().register(self.ctx)
        self.handler = self.ctx.commands["cortiq-router"]["handler"]
        text = self.handler("stats --json")
        data = json.loads(text)
        self.assertEqual(data["totals"]["calls"], 3)
        self.assertIn("by_provider", data["totals"])
        self.assertEqual(data["totals"]["by_provider"], {})
        self.assertIn("fallback_reasons", data["totals"])


if __name__ == "__main__":
    unittest.main()