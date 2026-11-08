"""Request-payload helpers (pure)."""

from __future__ import annotations

from typing import Any


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate to UTF-16 code units, the family's ``maxChars`` semantics."""
    return str(text or "")[:max_chars]


def _tool_result_text(block: dict) -> str:
    """Text inside an Anthropic ``tool_result`` block: a string or text parts."""
    inner = block.get("content")
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        return "\n".join(
            part["text"] for part in inner
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    return ""


def _parts_to_text(content: Any, *, tool_results: bool) -> str:
    """Join the text of a message body; ``tool_results`` also takes the text of tool_result blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("text", "input_text") and isinstance(part.get("text"), str):
            parts.append(part["text"])
        elif tool_results and part.get("type") == "tool_result":
            text = _tool_result_text(part)
            if text:
                parts.append(text)
    return "\n".join(parts)


def _content_to_text(content: Any) -> str:
    return _parts_to_text(content, tool_results=False)


def extract_last_user_text(payload: Any) -> str:
    """Last user text from Chat Completions messages or a Responses API request."""
    if isinstance(payload, dict) and isinstance(payload.get("input"), list):
        for item in reversed(payload["input"]):
            if (isinstance(item, dict) and item.get("type") == "message"
                    and item.get("role") == "user"):
                return _content_to_text(item.get("content"))
        return ""
    messages = payload.get("messages") if isinstance(payload, dict) else payload
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _content_to_text(message.get("content"))
    return ""


def extract_step_text(payload: Any) -> str:
    """Text the model has to act on in this call.

    The first call of a turn ends with the user's message, so this is that text.
    A tool-loop step ends with tool outputs — Anthropic ``tool_result`` blocks in a
    user message, Chat Completions ``role: tool`` messages, or Responses API
    ``function_call_output`` items — and those outputs are the step's text. The
    three shapes otherwise yield no text at all, which is why tool steps used to
    look like empty requests.
    """
    if isinstance(payload, dict) and isinstance(payload.get("input"), list):
        outputs = []
        for item in reversed(payload["input"]):
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                output = item.get("output")
                outputs.append(output if isinstance(output, str) else _parts_to_text(output, tool_results=False))
                continue
            break
        if outputs:
            return "\n".join(reversed(outputs))
        return extract_last_user_text(payload)
    messages = payload.get("messages") if isinstance(payload, dict) else payload
    if not isinstance(messages, list) or not messages:
        return ""
    last = messages[-1]
    if not isinstance(last, dict):
        return extract_last_user_text(messages)
    if last.get("role") == "tool":
        outputs = []
        for message in reversed(messages):
            if not (isinstance(message, dict) and message.get("role") == "tool"):
                break
            outputs.append(_content_to_text(message.get("content")))
        return "\n".join(reversed(outputs))
    if last.get("role") == "user":
        return _parts_to_text(last.get("content"), tool_results=True)
    return extract_last_user_text(messages)
