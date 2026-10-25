"""Request-payload helpers (pure)."""

from __future__ import annotations

from typing import Any


def truncate_text(text: str, max_chars: int) -> str:
    """Truncate to UTF-16 code units, the family's ``maxChars`` semantics."""
    return str(text or "")[:max_chars]


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in ("text", "input_text")
            and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def extract_last_user_text(messages: Any) -> str:
    """Text of the last ``role: "user"`` message from an OpenAI ``messages`` array."""
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _content_to_text(message.get("content"))
    return ""