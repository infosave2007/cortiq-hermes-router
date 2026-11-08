"""Unit tests for payload helpers (last user message extraction, truncation)."""

from __future__ import annotations

import unittest

from cortiq_router.routing import extract_last_user_text, extract_step_text, truncate_text


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


class ExtractStepTextTests(unittest.TestCase):
    """The text a call has to act on: the user's message first, then the tool outputs of each step."""

    def test_first_call_is_the_user_message(self):
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "fix it"}]
        self.assertEqual(extract_step_text(messages), "fix it")
        self.assertEqual(extract_step_text({"model": "m", "messages": messages}), "fix it")

    def test_anthropic_tool_results_in_a_user_message(self):
        messages = [
            {"role": "user", "content": "fix it"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "ls", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "file1\nfile2"},
                {"type": "tool_result", "tool_use_id": "b", "content": [
                    {"type": "text", "text": "ok"}, {"type": "image", "source": {}}]},
                {"type": "text", "text": "continue"},
            ]},
        ]
        self.assertEqual(extract_step_text(messages), "file1\nfile2\nok\ncontinue")
        self.assertEqual(extract_last_user_text(messages), "continue", "the old helper still ignores tool results")

    def test_tool_results_without_text_yield_nothing(self):
        messages = [
            {"role": "user", "content": "fix it"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                          "content": [{"type": "image", "source": {}}]}]},
        ]
        self.assertEqual(extract_step_text(messages), "")

    def test_openai_tool_messages_are_joined_in_order(self):
        messages = [
            {"role": "user", "content": "fix it"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}, {"id": "c2"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "first"},
            {"role": "tool", "tool_call_id": "c2", "content": [{"type": "text", "text": "second"}]},
        ]
        self.assertEqual(extract_step_text(messages), "first\nsecond")

    def test_responses_function_call_output(self):
        request = {"input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix it"}]},
            {"type": "function_call", "call_id": "c1", "name": "ls", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "file1"},
        ]}
        self.assertEqual(extract_step_text(request), "file1")
        request["input"].append({"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": "and now?"}]})
        self.assertEqual(extract_step_text(request), "and now?")

    def test_assistant_last_falls_back_to_the_last_user_text(self):
        messages = [{"role": "user", "content": "real"}, {"role": "assistant", "content": "prefill"}]
        self.assertEqual(extract_step_text(messages), "real")
        self.assertEqual(extract_step_text([]), "")
        self.assertEqual(extract_step_text(None), "")
        self.assertEqual(extract_step_text([{"role": "tool"}, "junk"]), "")


class TruncateTests(unittest.TestCase):
    def test_truncates_to_max_chars(self):
        self.assertEqual(truncate_text("abcdef", 3), "abc")
        self.assertEqual(truncate_text("🙂🙂🙂", 2), "🙂🙂")
        self.assertEqual(truncate_text(None, 5), "")
        self.assertEqual(truncate_text("short", 100), "short")


if __name__ == "__main__":
    unittest.main()