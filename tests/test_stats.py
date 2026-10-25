"""Tests for durable, text-free router decision statistics."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from cortiq_router.stats import StatsAggregator
from cortiq_router import stats as stats_module


def _load_plugin_module():
    root = Path(__file__).resolve().parents[1]
    name = "_cortiq_hermes_router_plugin_stats_test"
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load plugin module for tests")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PLUGIN = _load_plugin_module()

AT = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc).timestamp()


class MemoryState:
    def __init__(self, initial=None):
        self.data = copy.deepcopy(initial or {})
        self.writes = 0

    def get(self, key, default=None):
        return copy.deepcopy(self.data.get(key, default))

    def set(self, key, value):
        self.data[key] = copy.deepcopy(value)
        self.writes += 1


class FakeContext:
    def __init__(self):
        self.state = MemoryState()
        self.commands = {}
        self.middleware = {}
        self.config = {}
        self.capability = True
        self.profile_name = "test"

    def register_command(self, name, handler, description, args_hint):
        self.commands[name] = handler

    def register_middleware(self, name, handler):
        self.middleware[name] = handler

    def has_capability(self, name):
        return self.capability

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value


def decision(**overrides):
    value = {
        "task": "qa",
        "score": 0.25,
        "tier": "low",
        "model": "cheap/model",
        "originalModel": "original/model",
        "fallback": False,
        "status": "ok",
        "classifier_latency_ms": 100.0,
        "prompt": "must never be persisted",
    }
    value.update(overrides)
    return value


class AggregationTests(unittest.TestCase):
    def test_aggregates_dimensions_averages_latency_and_fallback(self):
        state = MemoryState()
        stats = StatsAggregator(state)
        stats.record(decision(), at=AT)
        stats.record(decision(score=0.75, classifier_latency_ms=300.0), at=AT)
        stats.record(decision(
            task="unclassified", score=None, tier=None,
            model="fallback/model", fallback=True,
            status="classifier_error", classifier_latency_ms=200.0,
        ), at=AT)

        snapshot = stats.snapshot()
        totals = snapshot["totals"]
        self.assertEqual(totals["calls"], 3)
        self.assertEqual(totals["by_tier"], {"low": 2, "(none)": 1})
        self.assertEqual(totals["by_task"], {"qa": 2, "unclassified": 1})
        self.assertEqual(totals["by_model"], {"cheap/model": 2, "fallback/model": 1})
        self.assertEqual(totals["by_status"], {"ok": 2, "classifier_error": 1})
        self.assertEqual(totals["fallback_count"], 1)
        self.assertAlmostEqual(totals["avg_score"], 0.5)
        self.assertAlmostEqual(totals["avg_classifier_latency_ms"], 200.0)
        self.assertEqual(totals["max_classifier_latency_ms"], 300.0)
        self.assertEqual(snapshot["daily"]["2026-10-25"], totals)
        self.assertEqual(snapshot["timezone"], "UTC")
        self.assertEqual(len(snapshot["recent"]), 3)
        self.assertNotIn("prompt", snapshot["recent"][0])
        self.assertNotIn("prompt", state.data["statistics"]["recent"][0])
        self.assertGreaterEqual(state.writes, 3)


class PersistenceAndWindowTests(unittest.TestCase):
    def test_recent_is_capped_at_200_and_returns_newest_first(self):
        stats = StatsAggregator(MemoryState())
        for index in range(205):
            stats.record(decision(model=f"model/{index}"), at=AT + index)

        snapshot = stats.snapshot()
        self.assertEqual(snapshot["totals"]["calls"], 205)
        self.assertEqual(len(snapshot["recent"]), 200)
        self.assertEqual(snapshot["recent"][0]["model"], "model/5")
        self.assertEqual(snapshot["recent"][-1]["model"], "model/204")
        self.assertEqual(
            [item["model"] for item in stats.recent(3)],
            ["model/204", "model/203", "model/202"],
        )

    def test_reset_clears_only_statistics(self):
        state = MemoryState({"last_decision": {"task": "keep-me"}})
        stats = StatsAggregator(state)
        stats.record(decision(), at=AT)
        stats.reset()

        snapshot = stats.snapshot()
        self.assertEqual(snapshot["totals"]["calls"], 0)
        self.assertEqual(snapshot["daily"], {})
        self.assertEqual(snapshot["recent"], [])
        self.assertEqual(state.data["last_decision"], {"task": "keep-me"})

    def test_old_last_decision_is_migrated_once_without_losing_it(self):
        old = {
            "at": AT,
            "task": "code",
            "score": 0.8,
            "tier": "high",
            "model": "strong/model",
            "candidates": ["strong/model"],
            "originalModel": "original/model",
            "fallback": False,
        }
        state = MemoryState({"last_decision": old})
        stats = StatsAggregator(state)

        snapshot = stats.snapshot()
        self.assertEqual(snapshot["totals"]["calls"], 1)
        self.assertEqual(snapshot["totals"]["by_status"], {"ok": 1})
        self.assertEqual(snapshot["recent"][0]["originalModel"], "original/model")
        self.assertIsNone(snapshot["recent"][0]["classifier_latency_ms"])
        self.assertEqual(state.data["last_decision"], old)
        self.assertNotIn("candidates", snapshot["recent"][0])

        reloaded = StatsAggregator(state).snapshot()
        self.assertEqual(reloaded["totals"]["calls"], 1)

    def test_utc_day_rollover_and_rolling_seven_day_week(self):
        stats = StatsAggregator(MemoryState())
        before = datetime(2026, 10, 24, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        after = datetime(2026, 10, 25, 0, 0, 1, tzinfo=timezone.utc).timestamp()
        seven_days_before = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc).timestamp()
        stats.record(decision(model="before"), at=before)
        stats.record(decision(model="after"), at=after)
        stats.record(decision(model="outside-week"), at=seven_days_before)

        self.assertEqual(set(stats.snapshot()["daily"]), {
            "2026-10-18", "2026-10-24", "2026-10-25",
        })
        self.assertEqual(stats.aggregate("today", now=AT)["calls"], 1)
        self.assertEqual(stats.aggregate("week", now=AT)["calls"], 2)
        self.assertEqual(stats.aggregate("all", now=AT)["calls"], 3)
        with self.assertRaises(ValueError):
            stats.aggregate("month", now=AT)

    def test_reload_restores_aggregates_and_recent(self):
        state = MemoryState()
        StatsAggregator(state).record(decision(), at=AT)
        restored = StatsAggregator(state).snapshot()
        self.assertEqual(restored["totals"]["calls"], 1)
        self.assertEqual(restored["recent"][0]["task"], "qa")

    def test_state_write_failure_does_not_lose_in_memory_decision(self):
        class BrokenState(MemoryState):
            def set(self, key, value):
                raise OSError("disk full")

        stats = StatsAggregator(BrokenState())
        stats.record(decision(), at=AT)
        self.assertEqual(stats.snapshot()["totals"]["calls"], 1)

    def test_concurrent_records_are_not_dropped(self):
        stats = StatsAggregator(MemoryState())
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: stats.record(
                decision(model=f"model/{index}"), at=AT + index,
            ), range(80)))
        snapshot = stats.snapshot()
        self.assertEqual(snapshot["totals"]["calls"], 80)
        self.assertEqual(len(snapshot["recent"]), 80)


class FormattingTests(unittest.TestCase):
    def test_summary_has_three_periods_and_all_breakdowns(self):
        stats = StatsAggregator(MemoryState())
        stats.record(decision(), at=AT)
        stats.record(decision(
            score=0.75, status="classifier_error", fallback=True,
            classifier_latency_ms=300.0,
        ), at=AT)

        output = stats_module.format_stats(stats, now=AT)
        self.assertIn("Period", output)
        self.assertIn("today", output)
        self.assertIn("week", output)
        self.assertIn("all", output)
        self.assertIn("Avg score", output)
        self.assertIn("0.5000", output)
        self.assertIn("By tier", output)
        self.assertIn("By task", output)
        self.assertIn("By model", output)
        self.assertIn("By status", output)
        self.assertIn("cheap/model", output)
        self.assertIn("classifier_error", output)

    def test_filtered_summary_and_empty_state(self):
        empty = StatsAggregator(MemoryState())
        self.assertIn("no decisions", stats_module.format_stats(empty, now=AT))
        empty.record(decision(), at=AT)
        output = stats_module.format_stats(empty, "today", now=AT)
        self.assertIn("Today", output)
        self.assertNotIn("All time ·", output)

    def test_recent_table_contains_decision_metadata(self):
        stats = StatsAggregator(MemoryState())
        stats.record(decision(fallback=True), at=AT)
        output = stats_module.format_recent(stats.recent(10))
        self.assertIn("2026-10-25T12:00:00.000Z", output)
        self.assertIn("original/model", output)
        self.assertIn("cheap/model", output)
        self.assertIn("yes", output)
        self.assertIn("100.0", output)
        self.assertIn("no decisions", stats_module.format_recent([]))


class PluginStatsCommandTests(unittest.TestCase):
    CONFIG = {
        "enabled": True,
        "maxChars": 12000,
        "globalTiers": {"low": ["cheap/model"]},
        "defaultModel": "fallback/model",
        "echoRouting": False,
    }
    CLASSIFIED = {
        "taskLabel": "qa",
        "complexityScore": 0.25,
        "complexityTier": "low",
        "requestId": "req_test",
        "source": "router",
    }

    def registered(self):
        ctx = FakeContext()
        PLUGIN.register(ctx)
        return ctx, ctx.commands["cortiq-router"], ctx.middleware["llm_request"]

    def test_empty_stats_json_recent_and_reset_confirmation(self):
        _ctx, command, _middleware = self.registered()
        payload = json.loads(command("stats --json"))
        self.assertEqual(payload["totals"]["calls"], 0)
        self.assertEqual(payload["timezone"], "UTC")
        self.assertIn("no decisions", command("stats recent"))
        self.assertIn("no decisions", command("stats today"))
        self.assertIn("reset yes", command("stats reset"))
        self.assertIn("statistics reset", command("stats reset yes"))
        self.assertIn("usage", command("stats recent 0"))

    def test_capability_probe_context_without_state_registers_in_memory(self):
        class ProbeContext(FakeContext):
            def __getattribute__(self, name):
                if name == "state":
                    raise AttributeError("state")
                return super().__getattribute__(name)

        ctx = ProbeContext()
        PLUGIN.register(ctx)
        payload = json.loads(ctx.commands["cortiq-router"]("stats --json"))
        self.assertEqual(payload["totals"]["calls"], 0)

    def test_middleware_records_metadata_and_stats_commands_expose_it(self):
        ctx, command, middleware = self.registered()
        request = {
            "model": "original/model",
            "messages": [{"role": "user", "content": "secret prompt"}],
        }
        with patch.object(PLUGIN, "load_config", return_value=self.CONFIG), \
                patch.object(PLUGIN, "classify", side_effect=AssertionError("legacy classifier path")), \
                patch.object(PLUGIN, "classify_detailed", create=True,
                             return_value=(self.CLASSIFIED, {
                                 "status": "ok", "classifier_latency_ms": 12.5,
                             })) as detailed:
            result = middleware(request)
            middleware(request)  # cached classification is still a middleware decision

        self.assertEqual(result["request"]["model"], "cheap/model")
        self.assertEqual(detailed.call_count, 1)
        payload = json.loads(command("stats --json"))
        self.assertEqual(payload["totals"]["calls"], 2)
        self.assertEqual(payload["totals"]["by_model"], {"cheap/model": 2})
        self.assertEqual(payload["totals"]["by_status"], {"ok": 2})
        self.assertEqual(payload["recent"][0]["originalModel"], "original/model")
        self.assertIsNone(payload["recent"][1]["classifier_latency_ms"])
        self.assertEqual(payload["recent"][0]["classifier_latency_ms"], 12.5)
        self.assertNotIn("secret prompt", json.dumps(payload))
        self.assertIn("original/model", command("stats recent 1"))
        self.assertIn("cheap/model", command("stats all"))
        self.assertIn("/cortiq-router stats", command("status"))
        self.assertIn("last_decision", ctx.state.data)

        command("stats reset")
        self.assertEqual(json.loads(command("stats --json"))["totals"]["calls"], 2)
        command("stats reset yes")
        self.assertEqual(json.loads(command("stats --json"))["totals"]["calls"], 0)
        self.assertIn("last_decision", ctx.state.data)

    def test_every_middleware_call_records_disabled_error_and_timeout_status(self):
        request = {
            "model": "original/model",
            "messages": [{"role": "user", "content": "classify this"}],
        }
        cases = [
            ("disabled", dict(self.CONFIG, enabled=False), None, True, request, "original/model", False),
            ("invalid_config", PLUGIN.ConfigError("bad config"), None, True, request, "original/model", True),
            ("classifier_timeout", self.CONFIG,
             (None, {"status": "classifier_timeout", "classifier_latency_ms": 50.0}),
             True, request, "fallback/model", True),
            ("disabled", self.CONFIG, None, False, request, "original/model", False),
            ("disabled", self.CONFIG, None, True,
             {"model": "original/model", "messages": []}, "original/model", False),
            ("disabled", self.CONFIG, None, True, None, None, False),
        ]
        for expected_status, config, classified, capability, incoming, model, fallback in cases:
            with self.subTest(status=expected_status, config=config, capability=capability, incoming=incoming):
                ctx, command, middleware = self.registered()
                ctx.capability = capability
                load = patch.object(
                    PLUGIN, "load_config",
                    side_effect=config if isinstance(config, Exception) else None,
                    return_value=None if isinstance(config, Exception) else config,
                )
                with load, patch.object(PLUGIN, "classify", side_effect=AssertionError("legacy path")), \
                        patch.object(PLUGIN, "classify_detailed", create=True,
                                     return_value=classified) as detailed:
                    middleware(incoming)
                entry = json.loads(command("stats --json"))["recent"][0]
                self.assertEqual(entry["status"], expected_status)
                self.assertEqual(entry["model"], model)
                self.assertEqual(entry["fallback"], fallback)
                self.assertEqual(entry["originalModel"], "original/model" if isinstance(incoming, dict) else None)
                if expected_status in ("disabled", "invalid_config"):
                    detailed.assert_not_called()
                if expected_status == "classifier_timeout":
                    self.assertEqual(entry["classifier_latency_ms"], 50.0)


if __name__ == "__main__":
    unittest.main()
