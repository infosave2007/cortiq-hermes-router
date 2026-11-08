"""Token usage of one provider response, in Anthropic-style buckets (pure).

``input_tokens`` are the uncached input tokens; cache reads and cache writes are
separate buckets, as Anthropic bills them. An OpenAI-shaped ``usage`` reports
``prompt_tokens`` including the cached part, so the cached tokens are taken out
of ``input_tokens`` here. Works on SDK objects and on plain dicts.
"""

from __future__ import annotations

from typing import Any, Optional

TOKEN_FIELDS: tuple[str, ...] = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _get(obj: Any, *names: str) -> Any:
    """First present, non-None attribute or key among ``names``."""
    if obj is None:
        return None
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def usage_from_response(response: Any) -> Optional[dict[str, int]]:
    """``{input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}`` or ``None``.

    ``None`` means the response carries no usage at all (a stream wrapper, an
    error object, a plain string); a response with usage of all zeros is kept.
    """
    usage = _get(response, "usage")
    if usage is None or isinstance(usage, (str, bytes, int, float, bool)):
        return None
    details = _get(usage, "prompt_tokens_details", "input_tokens_details")
    cache_read = _get(usage, "cache_read_input_tokens", "cache_read_tokens", "prompt_cache_hit_tokens")
    if cache_read is None:
        cache_read = _get(details, "cached_tokens")
    cache_write = _get(usage, "cache_creation_input_tokens", "cache_write_tokens")
    if cache_write is None:
        cache_write = _get(details, "cache_write_tokens", "cache_creation_input_tokens", "cache_creation_tokens")
    output = _get(usage, "completion_tokens", "output_tokens")
    prompt = _get(usage, "prompt_tokens")
    anthropic_input = _get(usage, "input_tokens")
    if prompt is not None and (anthropic_input is None or _get(usage, "completion_tokens") is not None):
        # OpenAI shape: prompt_tokens is the whole input, cached part included.
        input_tokens = max(_count(prompt) - _count(cache_read), 0)
    else:
        input_tokens = _count(anthropic_input)
    if prompt is None and anthropic_input is None and output is None and cache_read is None and cache_write is None:
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": _count(output),
        "cache_read_tokens": _count(cache_read),
        "cache_write_tokens": _count(cache_write),
    }
