"""Unit tests for the token-usage parser (pure, no network)."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from cortiq_router.usage import usage_from_response


class UsageFromResponseTests(unittest.TestCase):
    def test_anthropic_message_object(self):
        response = SimpleNamespace(usage=SimpleNamespace(
            input_tokens=100, output_tokens=20, cache_read_input_tokens=300, cache_creation_input_tokens=5))
        self.assertEqual(usage_from_response(response), {
            "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 300, "cache_write_tokens": 5})

    def test_anthropic_dict_without_cache_fields(self):
        self.assertEqual(usage_from_response({"usage": {"input_tokens": 7, "output_tokens": 3}}), {
            "input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 0, "cache_write_tokens": 0})

    def test_openai_shape_takes_cached_tokens_out_of_the_input(self):
        response = SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=120, completion_tokens=8,
            prompt_tokens_details=SimpleNamespace(cached_tokens=100, cache_write_tokens=None)))
        self.assertEqual(usage_from_response(response), {
            "input_tokens": 20, "output_tokens": 8, "cache_read_tokens": 100, "cache_write_tokens": 0})
        plain = {"usage": {"prompt_tokens": 50, "completion_tokens": 5}}
        self.assertEqual(usage_from_response(plain)["input_tokens"], 50)

    def test_proxy_shape_with_anthropic_cache_fields_on_a_chat_usage(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 10, "cache_read_input_tokens": 900,
                 "cache_creation_input_tokens": 50}
        self.assertEqual(usage_from_response({"usage": usage}), {
            "input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 900, "cache_write_tokens": 50})

    def test_hermes_canonical_names(self):
        usage = {"input_tokens": 40, "output_tokens": 2, "cache_read_tokens": 10, "cache_write_tokens": 1}
        self.assertEqual(usage_from_response(SimpleNamespace(usage=usage)), usage)

    def test_no_usage_is_none(self):
        for response in (None, "text", 42, object(), {"choices": []}, SimpleNamespace(usage=None),
                         {"usage": "n/a"}, {"usage": {}}):
            with self.subTest(response=response):
                self.assertIsNone(usage_from_response(response))

    def test_negative_bool_and_float_counts_are_sanitized(self):
        usage = {"input_tokens": -1, "output_tokens": True, "cache_read_input_tokens": 2.9}
        self.assertEqual(usage_from_response({"usage": usage}), {
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 2, "cache_write_tokens": 0})


if __name__ == "__main__":
    unittest.main()
