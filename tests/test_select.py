"""Unit tests for the pure selection logic (tiers / bands / taskRules / fallback)."""

from __future__ import annotations

import unittest

from cortiq_router.select import (
    candidates_for,
    parse_task_rule,
    resolve_tier,
    select_model,
)


def config(**overrides):
    base = {
        "globalTiers": {
            "low": ["cheap/small"],
            "medium": ["mid/one", "mid/two"],
            "high": ["strong/one"],
        },
        "taskRules": {},
        "defaultModel": "fallback/model",
        "complexityBands": None,
    }
    base.update(overrides)
    return base


class ResolveTierTests(unittest.TestCase):
    def test_no_bands_passes_router_tier_through(self):
        self.assertEqual(resolve_tier(0.9, "high", None), "high")
        self.assertEqual(resolve_tier(0.1, "medium", None), "medium")

    def test_bands_are_inclusive_on_upper_bound(self):
        bands = {"low": 0.35, "medium": 0.65}
        self.assertEqual(resolve_tier(0.0, "high", bands), "low")
        self.assertEqual(resolve_tier(0.35, "high", bands), "low")
        self.assertEqual(resolve_tier(0.350001, "high", bands), "medium")
        self.assertEqual(resolve_tier(0.65, "high", bands), "medium")
        self.assertEqual(resolve_tier(0.650001, "high", bands), "high")
        self.assertEqual(resolve_tier(1.0, "low", bands), "high")

    def test_bands_ignore_router_tier(self):
        bands = {"low": 0.35, "medium": 0.65}
        self.assertEqual(resolve_tier(0.1, "high", bands), "low")


class ParseTaskRuleTests(unittest.TestCase):
    def test_string_becomes_any(self):
        self.assertEqual(parse_task_rule("a/b"), {"any": ["a/b"], "per": {}})

    def test_array_becomes_any(self):
        self.assertEqual(parse_task_rule(["a/b", "c/d"]), {"any": ["a/b", "c/d"], "per": {}})

    def test_tier_object_with_any_and_models_alias(self):
        rule = parse_task_rule({"low": "cheap/x", "high": ["strong/x"], "any": ["mid/x"]})
        self.assertEqual(rule["per"]["low"], ["cheap/x"])
        self.assertEqual(rule["per"]["high"], ["strong/x"])
        self.assertEqual(rule["any"], ["mid/x"])
        alias = parse_task_rule({"models": ["only/x"]})
        self.assertEqual(alias["any"], ["only/x"])

    def test_none_is_empty(self):
        self.assertEqual(parse_task_rule(None), {"any": [], "per": {}})


class CandidatesTests(unittest.TestCase):
    def test_priority_task_tier_then_global_then_default(self):
        cfg = config(taskRules={"coding": {"medium": ["task/medium"]}})
        models, matched = candidates_for(cfg, "coding", "medium")
        self.assertEqual(models, ["task/medium", "mid/one", "mid/two", "fallback/model"])
        self.assertTrue(matched)

    def test_high_escalation_global_high_precedes_task_any(self):
        cfg = config(taskRules={"coding": {"high": ["task/high"], "any": ["task/any"]}})
        models, _ = candidates_for(cfg, "coding", "high")
        self.assertEqual(models, ["task/high", "strong/one", "task/any", "fallback/model"])

    def test_low_task_any_precedes_global_chain(self):
        cfg = config(taskRules={"chitchat": {"any": ["task/any"]}})
        models, matched = candidates_for(cfg, "chitchat", "low")
        self.assertEqual(models, ["task/any", "cheap/small", "fallback/model"])
        self.assertTrue(matched)

    def test_dedup_and_empty_entries_dropped(self):
        cfg = config(globalTiers={"low": ["a/b", "a/b", "", "fallback/model"]})
        models, _ = candidates_for(cfg, "anything", "low")
        self.assertEqual(models, ["a/b", "fallback/model"])

    def test_missing_tier_chain_falls_to_default(self):
        cfg = config(globalTiers={"low": ["a/b"]})
        models, _ = candidates_for(cfg, "x", "high")
        self.assertEqual(models, ["fallback/model"])

    def test_no_rules_no_tier_no_default_is_empty(self):
        models, matched = candidates_for({"globalTiers": {}, "taskRules": {}, "defaultModel": ""},
                                         "x", "low")
        self.assertEqual(models, [])
        self.assertFalse(matched)

    def test_unknown_task_label_uses_global_chain(self):
        models, matched = candidates_for(config(), "unknown/task", "low")
        self.assertEqual(models, ["cheap/small", "fallback/model"])
        self.assertFalse(matched)


class SelectModelTests(unittest.TestCase):
    def test_classifier_error_falls_back_to_default_model(self):
        selection = select_model(config(), None)
        self.assertEqual(selection["model"], "fallback/model")
        self.assertEqual(selection["task"], "unclassified")
        self.assertIsNone(selection["score"])
        self.assertTrue(selection["fallback"])
        self.assertEqual(selection["candidates"], ["fallback/model"])

    def test_classifier_error_without_default_keeps_caller_model(self):
        selection = select_model(config(defaultModel=""), None)
        self.assertEqual(selection["model"], "")
        self.assertTrue(selection["fallback"])

    def test_classified_request_uses_bands_and_task_rules(self):
        cfg = config(complexityBands={"low": 0.35, "medium": 0.65},
                     taskRules={"coding": {"high": ["coder/x"]}})
        decision = {"taskLabel": "coding", "complexityScore": 0.82,
                    "complexityTier": "medium", "confidence": 0.9,
                    "requestId": "req_1", "isNovel": False, "source": "router"}
        selection = select_model(cfg, decision)
        # bands override the router tier: 0.82 -> high -> task high rule wins
        self.assertEqual(selection["tier"], "high")
        self.assertEqual(selection["model"], "coder/x")
        self.assertEqual(selection["task"], "coding")
        self.assertAlmostEqual(selection["score"], 0.82)
        self.assertFalse(selection["fallback"])
        self.assertTrue(selection["taskRuleMatched"])

    def test_router_tier_used_without_bands(self):
        cfg = config()
        decision = {"taskLabel": "chitchat", "complexityScore": 0.1,
                    "complexityTier": "high", "confidence": 0.9,
                    "requestId": "req_2", "isNovel": False, "source": "router"}
        selection = select_model(cfg, decision)
        self.assertEqual(selection["tier"], "high")
        self.assertEqual(selection["model"], "strong/one")


if __name__ == "__main__":
    unittest.main()