"""Unit tests for the classifier client: payload shape, response validation, degradation."""

from __future__ import annotations

import importlib
import io
import json
import unittest
import urllib.error

from cortiq_router.classify import build_route_body, classify, parse_route_decision
classify_module = importlib.import_module("cortiq_router.classify")

# Shape fixture: matches a real AllaiGate /v1/route response (schema 1.1 subset).
REAL_DECISION = {
    "schema_version": "1.1",
    "request_id": "req_livefixture0001",
    "decision": {
        "task_id": 7,
        "task_label": "coding",
        "taxonomy_id": "data-assistant",
        "confidence": 0.8741,
        "confident": True,
        "raw_confidence": 0.8741,
        "margin": 0.41,
        "is_novel": False,
        "novelty_score": 0.02,
        "complexity": {"score": 0.8213, "tier": "high"},
        "source": "router",
        "flags": [],
    },
    "scores": [],
    "usage": {"billable_decisions": 1, "oracle_calls": 0},
    "meta": {"model_version": "m1", "taxonomy_version": "t1", "latency_ms": 42,
             "embedding_latency_ms": 6, "served_by": "test"},
}


class BuildRouteBodyTests(unittest.TestCase):
    def test_input_is_a_struct_not_a_string(self):
        body = build_route_body("hello")
        self.assertIsInstance(body["input"], dict)
        self.assertEqual(body["input"], {"text": "hello"})

    def test_profile_and_taxonomy_and_truncation(self):
        body = build_route_body("x" * 50, profile="cost-saver",
                                taxonomy_id="data-assistant", max_chars=10)
        self.assertEqual(body["options"]["policy_profile"], "cost-saver")
        self.assertEqual(body["taxonomy_id"], "data-assistant")
        self.assertEqual(body["input"]["text"], "x" * 10)

    def test_defaults(self):
        body = build_route_body("p")
        self.assertEqual(body["options"]["policy_profile"], "balanced")
        self.assertNotIn("taxonomy_id", body)
        self.assertTrue(body["options"]["allow_oracle"])

    def test_utf16_truncation_semantics(self):
        body = build_route_body("🙂🙂🙂", max_chars=2)
        self.assertEqual(body["input"]["text"], "🙂🙂")


class ParseRouteDecisionTests(unittest.TestCase):
    def test_valid_fixture(self):
        decision = parse_route_decision(REAL_DECISION)
        self.assertIsNotNone(decision)
        self.assertEqual(decision["taskLabel"], "coding")
        self.assertAlmostEqual(decision["complexityScore"], 0.8213)
        self.assertEqual(decision["complexityTier"], "high")
        self.assertEqual(decision["requestId"], "req_livefixture0001")
        self.assertEqual(decision["source"], "router")
        self.assertFalse(decision["isNovel"])

    def test_rejects_malformed(self):
        cases = [
            None, "string", {}, {"decision": None},
            {"decision": {}},
            {"decision": {"complexity": {"score": 0.5, "tier": "ultra"}, "task_label": "x",
                          "confidence": 0.5, "is_novel": False, "source": "router"}},
            {"decision": {"complexity": {"score": 1.5, "tier": "high"}, "task_label": "x",
                          "confidence": 0.5, "is_novel": False, "source": "router"}},
            {"decision": {"complexity": {"score": 0.5, "tier": "high"}, "task_label": "x",
                          "confidence": 0.5, "is_novel": "no", "source": "router"}},
            {"decision": {"complexity": {"score": 0.5, "tier": "low"}, "task_label": "  ",
                          "confidence": 0.5, "is_novel": False, "source": "router"}},
            {"decision": {"complexity": {"score": 0.5, "tier": "low"}, "task_label": "x",
                          "confidence": 0.5, "is_novel": False, "source": "router"}},
            {"request_id": "r1", "decision": {"complexity": {"score": 0.5, "tier": "medium"},
                                              "task_label": "x", "confidence": 0.5,
                                              "is_novel": True, "source": ""}},
        ]
        for case in cases:
            self.assertIsNone(parse_route_decision(case), msg=repr(case)[:120])


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, payload=None, error=None, expect_body=None):
        self._payload = payload
        self._error = error
        self.expect_body = expect_body
        self.last_request = None

    def open(self, request, timeout=None):
        self.last_request = request
        self.last_timeout = timeout
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._payload)


class ClassifyTests(unittest.TestCase):
    CONFIG = {"apiKeyEnv": "CORTIQ_ROUTER_KEY", "routerUrl": "https://router.allaigate.com",
              "taxonomyId": "data-assistant", "timeoutMs": 15000, "maxChars": 12000,
              "routerProfile": "balanced"}

    def test_missing_key_degrades_with_log(self):
        logs = []
        result = classify("hi", self.CONFIG, env={}, log=logs.append)
        self.assertIsNone(result)
        self.assertIn("CORTIQ_ROUTER_KEY is not set", logs[0])

    def test_successful_classification_sends_struct_payload(self):
        opener = _FakeOpener(payload=REAL_DECISION)
        result = classify("write a function", self.CONFIG, env={"CORTIQ_ROUTER_KEY": "test-key"},
                          log=lambda _msg: None, opener=opener)
        self.assertEqual(result["taskLabel"], "coding")
        sent = json.loads(opener.last_request.data)
        self.assertIsInstance(sent["input"], dict)
        self.assertEqual(sent["input"]["text"], "write a function")
        self.assertEqual(sent["options"]["policy_profile"], "balanced")
        self.assertEqual(opener.last_request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(opener.last_timeout, 15.0)

    def test_http_error_degrades(self):
        error = urllib.error.HTTPError(
            "https://router.allaigate.com/v1/route", 422, "Unprocessable", {}, None)
        opener = _FakeOpener(error=error)
        self.addCleanup(error.close)
        logs = []
        result = classify("x", self.CONFIG, env={"CORTIQ_ROUTER_KEY": "k"},
                          log=logs.append, opener=opener)
        self.assertIsNone(result)
        self.assertIn("HTTP 422", logs[0])

    def test_network_error_degrades(self):
        opener = _FakeOpener(error=TimeoutError("timed out"))
        logs = []
        result = classify("x", self.CONFIG, env={"CORTIQ_ROUTER_KEY": "k"},
                          log=logs.append, opener=opener)
        self.assertIsNone(result)
        self.assertIn("unreachable", logs[0])

    def test_invalid_body_degrades(self):
        opener = _FakeOpener(payload={"unexpected": True})
        logs = []
        result = classify("x", self.CONFIG, env={"CORTIQ_ROUTER_KEY": "k"},
                          log=logs.append, opener=opener)
        self.assertIsNone(result)
        self.assertIn("invalid classifier response", logs[0])

    def test_real_transport_error_path_never_raises(self):
        # urllib raises HTTPError for non-2xx when using the default opener path
        class _Boom(_FakeOpener):
            def open(self, request, timeout=None):
                raise urllib.error.URLError("connection refused")

        logs = []
        result = classify("x", self.CONFIG, env={"CORTIQ_ROUTER_KEY": "k"},
                          log=logs.append, opener=_Boom())
        self.assertIsNone(result)
        self.assertTrue(logs)


class ClassifyMetadataTests(unittest.TestCase):
    CONFIG = ClassifyTests.CONFIG

    def test_success_has_ok_status_and_latency(self):
        result, metadata = classify_module.classify_detailed(
            "x", self.CONFIG, env={"CORTIQ_ROUTER_KEY": "k"},
            log=lambda _msg: None, opener=_FakeOpener(payload=REAL_DECISION),
        )
        self.assertEqual(result["taskLabel"], "coding")
        self.assertEqual(metadata["status"], "ok")
        self.assertGreaterEqual(metadata["classifier_latency_ms"], 0)

    def test_timeout_has_distinct_status(self):
        opener = _FakeOpener(error=TimeoutError("timed out"))
        result, metadata = classify_module.classify_detailed(
            "x", self.CONFIG, env={"CORTIQ_ROUTER_KEY": "k"},
            log=lambda _msg: None, opener=opener,
        )
        self.assertIsNone(result)
        self.assertEqual(metadata["status"], "classifier_timeout")
        self.assertGreaterEqual(metadata["classifier_latency_ms"], 0)

    def test_url_error_timeout_reason_has_distinct_status(self):
        opener = _FakeOpener(error=urllib.error.URLError(TimeoutError("timed out")))
        _result, metadata = classify_module.classify_detailed(
            "x", self.CONFIG, env={"CORTIQ_ROUTER_KEY": "k"},
            log=lambda _msg: None, opener=opener,
        )
        self.assertEqual(metadata["status"], "classifier_timeout")

    def test_http_error_and_missing_key_are_classifier_errors(self):
        error = urllib.error.HTTPError(
            "https://router.allaigate.com/v1/route", 500, "Server", {}, None)
        opener = _FakeOpener(error=error)
        self.addCleanup(error.close)
        _result, metadata = classify_module.classify_detailed(
            "x", self.CONFIG, env={"CORTIQ_ROUTER_KEY": "k"},
            log=lambda _msg: None, opener=opener,
        )
        self.assertEqual(metadata["status"], "classifier_error")

        _result, metadata = classify_module.classify_detailed(
            "x", self.CONFIG, env={}, log=lambda _msg: None,
        )
        self.assertEqual(metadata["status"], "classifier_error")
        self.assertIsNone(metadata["classifier_latency_ms"])


if __name__ == "__main__":
    unittest.main()