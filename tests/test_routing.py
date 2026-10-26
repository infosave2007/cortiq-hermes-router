"""Unit tests for payload helpers (last user message extraction, truncation)."""

from __future__ import annotations

import unittest

from cortiq_router.routing import extract_last_user_text, truncate_text


class ExtractLastUserTextTests(unittest.TestCase):
    def test_last_user_wins(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second question"},
        ]
        self.assertEqual(extract_last_user_text(messages), "second question")

    def test_array_content_joins_text_parts_only(self):
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "part one"},
                {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
                {"type": "text", "text": "part two"},
            ]},
        ]
        self.assertEqual(extract_last_user_text(messages), "part one\npart two")

    def test_skips_assistant_and_tool_messages(self):
        messages = [
            {"role": "user", "content": "real"},
            {"role": "tool", "content": "tool output"},
            {"role": "assistant", "content": "assistant text"},
        ]
        self.assertEqual(extract_last_user_text(messages), "real")

    def test_no_user_message(self):
        self.assertEqual(extract_last_user_text([{"role": "assistant", "content": "a"}]), "")
        self.assertEqual(extract_last_user_text([]), "")
        self.assertEqual(extract_last_user_text(None), "")
        self.assertEqual(extract_last_user_text("not a list"), "")

    def test_responses_input_uses_last_user_message(self):
        request = {
            "instructions": "system",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]},
                {"type": "message", "role": "assistant", "content": "answer"},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "last"}]},
            ],
        }
        self.assertEqual(extract_last_user_text(request), "last")

    def test_non_string_content_is_empty(self):
        self.assertEqual(extract_last_user_text([{"role": "user", "content": {"x": 1}}]), "")


class TruncateTests(unittest.TestCase):
    def test_truncates_to_max_chars(self):
        self.assertEqual(truncate_text("abcdef", 3), "abc")
        self.assertEqual(truncate_text("🙂🙂🙂", 2), "🙂🙂")
        self.assertEqual(truncate_text(None, 5), "")
        self.assertEqual(truncate_text("short", 100), "short")


if __name__ == "__main__":
    unittest.main()