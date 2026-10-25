"""Direct execution tests: request builders, reply parsing, transport failures (mock server only)."""

from __future__ import annotations

import json
import unittest

from cortiq_router.execution import (
    ExecutionError,
    build_anthropic_request,
    build_openai_request,
    call_endpoint,
    parse_anthropic,
    parse_openai_json,
    probe_models,
    run_probe,
    to_anthropic_messages,
)
from tests.mock_server import MOCK_KEY, MockServer

KEY_ENV = "CORTIQ_TEST_EXEC_KEY"
ENV_WITH_KEY = {KEY_ENV: MOCK_KEY}


def endpoint(base_url: str, model: str, **overrides) -> dict:
    entry = {
        "provider": "ollama", "model": model, "baseUrl": base_url, "apiKeyEnv": None,
        "apiMode": "chat_completions", "timeoutMs": 2000, "extraBody": {}, "direct": True,
    }
    entry.update(overrides)
    return entry


REQUEST = {
    "model": "ignored-by-entry",
    "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "ping"}],
    "tools": [{"type": "function", "function": {"name": "lookup", "description": "find",
                                                "parameters": {"type": "object"}}}],
    "extra_body": {"provider": {"order": ["openrouter-only"]}},  # Hermes/OpenRouter knob
    "timeout": 30,
    "stream": True,
    "temperature": 0.2,
}


class OpenAIBuilderTests(unittest.TestCase):
    def test_body_uses_entry_model_forces_stream_and_drops_sdk_keys(self):
        entry = endpoint("http://127.0.0.1:1/v1", "qwen3:8b",
                         extraBody={"top_k": 20})
        url, headers, payload = build_openai_request(entry, REQUEST, None)
        body = json.loads(payload)
        self.assertEqual(url, "http://127.0.0.1:1/v1/chat/completions")
        self.assertEqual(body["model"], "qwen3:8b")
        self.assertTrue(body["stream"])
        self.assertEqual(body["top_k"], 20)
        self.assertNotIn("extra_body", body)
        self.assertNotIn("timeout", body)
        self.assertNotIn("Authorization", headers)

    def test_bearer_header_only_when_key_resolved(self):
        entry = endpoint("https://example.test/v1", "gpt-x", apiKeyEnv=KEY_ENV)
        _url, headers, _payload = build_openai_request(entry, REQUEST, MOCK_KEY)
        self.assertEqual(headers["Authorization"], f"Bearer {MOCK_KEY}")

    def test_missing_key_env_is_a_fallback_reason_not_a_crash(self):
        entry = endpoint("https://example.test/v1", "gpt-x", apiKeyEnv=KEY_ENV)
        with self.assertRaises(ExecutionError) as caught:
            call_endpoint(entry, REQUEST, env={})
        self.assertEqual(caught.exception.reason, "missing_key")
        self.assertNotIn(MOCK_KEY, str(caught.exception))


class AnthropicConversionTests(unittest.TestCase):
    def test_system_tool_use_and_tool_result_round_trip(self):
        system, messages = to_anthropic_messages([
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "find cats"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_9", "type": "function",
                "function": {"name": "lookup", "arguments": "{\"q\": \"cats\"}"}}]},
            {"role": "tool", "tool_call_id": "call_9", "content": "3 results"},
            {"role": "user", "content": "thanks"},
        ])
        self.assertEqual(system, "be brief")
        self.assertEqual(messages[1]["content"][0], {
            "type": "tool_use", "id": "call_9", "name": "lookup", "input": {"q": "cats"}})
        self.assertEqual(messages[2]["role"], "user")
        self.assertEqual(messages[2]["content"][0]["type"], "tool_result")
        self.assertEqual(messages[2]["content"][0]["tool_use_id"], "call_9")
        self.assertEqual(messages[2]["content"][1], {"type": "text", "text": "thanks"})

    def test_request_maps_tools_choice_and_default_max_tokens(self):
        entry = endpoint("https://api.anthropic.com", "claude-haiku", apiMode="anthropic_messages")
        url, headers, payload = build_anthropic_request(entry, REQUEST, MOCK_KEY)
        body = json.loads(payload)
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], MOCK_KEY)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(body["max_tokens"], 4096)
        self.assertEqual(body["system"], "be brief")
        self.assertEqual(body["tools"][0]["name"], "lookup")
        self.assertIn("input_schema", body["tools"][0])
        self.assertEqual(body["temperature"], 0.2)
        self.assertNotIn("stream", body)

    def test_unsupported_image_part_is_rejected_for_anthropic(self):
        with self.assertRaises(ExecutionError) as caught:
            to_anthropic_messages([{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}])
        self.assertEqual(caught.exception.reason, "unsupported_request")

    def test_anthropic_reply_maps_to_openai_shape(self):
        reply = parse_anthropic({
            "id": "msg_1", "model": "claude-haiku", "stop_reason": "tool_use",
            "content": [{"type": "text", "text": "let me check"},
                        {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"q": "x"}}],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }, "claude-haiku")
        choice = reply.choices[0]
        self.assertEqual(choice.finish_reason, "tool_calls")
        self.assertEqual(choice.message.content, "let me check")
        self.assertEqual(choice.message.tool_calls[0].function.name, "lookup")
        self.assertEqual(json.loads(choice.message.tool_calls[0].function.arguments), {"q": "x"})
        self.assertEqual(reply.usage.total_tokens, 6)


class ReplyParsingTests(unittest.TestCase):
    def test_openai_json_without_choices_is_invalid_response(self):
        with self.assertRaises(ExecutionError) as caught:
            parse_openai_json({"error": "nope"}, "m")
        self.assertEqual(caught.exception.reason, "invalid_response")

    def test_empty_message_is_empty_response(self):
        with self.assertRaises(ExecutionError) as caught:
            parse_openai_json({"choices": [{"message": {"content": ""}, "finish_reason": None}]}, "m")
        self.assertEqual(caught.exception.reason, "empty_response")


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.server = MockServer().__enter__()

    def tearDown(self):
        self.server.__exit__(None, None, None)

    def test_sse_happy_path_assembles_text(self):
        reply = call_endpoint(endpoint(self.server.base_url, "qwen3:8b"), REQUEST, ENV_WITH_KEY)
        self.assertEqual(reply.choices[0].message.content, "pong from local")
        self.assertEqual(reply.choices[0].finish_reason, "stop")
        sent = self.server.seen[-1]["body"]
        self.assertTrue(sent["stream"])
        self.assertEqual(sent["model"], "qwen3:8b")

    def test_plain_json_reply_is_accepted_too(self):
        reply = call_endpoint(endpoint(self.server.base_url, "plain-json"), REQUEST, ENV_WITH_KEY)
        self.assertEqual(reply.choices[0].message.content, "pong from plain json")
        self.assertEqual(reply.usage.prompt_tokens, 7)

    def test_streamed_tool_call_arguments_are_joined(self):
        reply = call_endpoint(endpoint(self.server.base_url, "tool-call"), REQUEST, ENV_WITH_KEY)
        call = reply.choices[0].message.tool_calls[0]
        self.assertEqual(call.id, "call_1")
        self.assertEqual(call.function.name, "lookup")
        self.assertEqual(json.loads(call.function.arguments), {"q": "cats"})
        self.assertEqual(reply.choices[0].finish_reason, "tool_calls")

    def test_reasoning_content_is_kept_separate_from_content(self):
        reply = call_endpoint(endpoint(self.server.base_url, "reasoning"), REQUEST, ENV_WITH_KEY)
        self.assertEqual(reply.choices[0].message.reasoning_content, "thinking…")
        self.assertEqual(reply.choices[0].message.content, "pong from reasoning")

    def test_bearer_key_is_sent_and_checked_by_the_server(self):
        ok = call_endpoint(endpoint(self.server.base_url, "needs-auth", apiKeyEnv=KEY_ENV),
                           REQUEST, ENV_WITH_KEY)
        self.assertEqual(ok.choices[0].message.content, "pong from local")
        with self.assertRaises(ExecutionError) as caught:
            call_endpoint(endpoint(self.server.base_url, "needs-auth"), REQUEST, {})
        self.assertEqual(caught.exception.reason, "http_401")

    def test_anthropic_direct_round_trip_with_tools(self):
        entry = endpoint(self.server.base_url.replace("/v1", ""), "claude-haiku",
                         apiMode="anthropic_messages", apiKeyEnv=KEY_ENV)
        reply = call_endpoint(entry, REQUEST, ENV_WITH_KEY)
        self.assertEqual(reply.choices[0].finish_reason, "tool_calls")
        self.assertEqual(reply.choices[0].message.tool_calls[0].function.name, "lookup")
        sent = self.server.seen[-1]
        self.assertEqual(sent["path"], "/v1/messages")
        self.assertEqual(sent["headers"].get("x-api-key"), MOCK_KEY)

    def test_failure_modes_map_to_stable_reasons(self):
        cases = {
            "http-500": "http_500",
            "bad-json": "invalid_json",
            "bad-stream": "invalid_stream",
            "stream-error": "stream_error",
            "empty": "empty_response",
        }
        for model, reason in cases.items():
            with self.subTest(model=model):
                with self.assertRaises(ExecutionError) as caught:
                    call_endpoint(endpoint(self.server.base_url, model), REQUEST, ENV_WITH_KEY)
                self.assertEqual(caught.exception.reason, reason)

    def test_timeout_is_reported_as_timeout(self):
        with self.assertRaises(ExecutionError) as caught:
            call_endpoint(endpoint(self.server.base_url, "slow", timeoutMs=300), REQUEST, ENV_WITH_KEY)
        self.assertEqual(caught.exception.reason, "timeout")

    def test_connection_refused_is_reported_as_connection(self):
        with self.assertRaises(ExecutionError) as caught:
            call_endpoint(endpoint("http://127.0.0.1:9/v1", "qwen3:8b", timeoutMs=500), REQUEST, {})
        self.assertEqual(caught.exception.reason, "connection")

    def test_probe_models_lists_models_without_completions(self):
        result = probe_models(endpoint(self.server.base_url, "qwen3:8b"), env={}, timeout=2.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], 2)
        self.assertTrue(result["modelListed"])
        self.assertEqual(self.server.seen[-1]["method"], "GET")
        self.assertTrue(all(item["method"] == "GET" or item["path"] == "/v1/chat/completions"
                            for item in self.server.seen))

    def test_probe_reports_missing_model_and_secret_free_failure(self):
        result = probe_models(endpoint(self.server.base_url, "not-installed", apiKeyEnv=KEY_ENV),
                              env={}, timeout=2.0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing_key")
        ok = probe_models(endpoint(self.server.base_url, "not-installed"), env={}, timeout=2.0)
        self.assertTrue(ok["ok"])
        self.assertFalse(ok["modelListed"])

    def test_run_probe_is_one_small_completion(self):
        result = run_probe(endpoint(self.server.base_url, "qwen3:8b"), "Reply with one word: pong", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"], "pong from local")
        sent = self.server.seen[-1]["body"]
        self.assertEqual(sent["max_tokens"], 8)
        self.assertEqual(len(sent["messages"]), 1)


if __name__ == "__main__":
    unittest.main()
