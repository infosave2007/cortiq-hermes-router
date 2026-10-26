"""Provider presets, normalized tier entries, and secret-free config inspection."""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Iterator, Mapping, Optional


# Only endpoint metadata and environment-variable NAMES live here.  Credentials
# are resolved at call time and are never added to a normalized entry.
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "openrouter": {"baseUrl": "https://openrouter.ai/api/v1", "apiKeyEnv": "OPENROUTER_API_KEY"},
    "ollama": {"baseUrl": "http://127.0.0.1:11434/v1"},
    "lmstudio": {"baseUrl": "http://127.0.0.1:1234/v1"},
    "cortiq": {"baseUrl": "http://127.0.0.1:8080/v1"},
    "openai": {"baseUrl": "https://api.openai.com/v1", "apiKeyEnv": "OPENAI_API_KEY"},
    "deepseek": {"baseUrl": "https://api.deepseek.com/v1", "apiKeyEnv": "DEEPSEEK_API_KEY"},
    "groq": {"baseUrl": "https://api.groq.com/openai/v1", "apiKeyEnv": "GROQ_API_KEY"},
    "together": {"baseUrl": "https://api.together.xyz/v1", "apiKeyEnv": "TOGETHER_API_KEY"},
    "anthropic": {"baseUrl": "https://api.anthropic.com", "apiKeyEnv": "ANTHROPIC_API_KEY"},
}

ENTRY_FIELDS = frozenset({"provider", "model", "native", "baseUrl", "apiKeyEnv", "apiMode", "timeoutMs", "extraBody"})
API_MODES = frozenset({"chat_completions", "anthropic_messages"})
DEFAULT_PROVIDER_TIMEOUT_MS = 60000


def is_provider_entry(value: Any) -> bool:
    """Distinguish one endpoint record from a taskRules tier mapping."""
    return isinstance(value, dict) and ("model" in value or "provider" in value)


def normalize_entry(value: Any) -> dict[str, Any]:
    """Return a JSON-safe endpoint record; strings use the current Hermes provider."""
    if isinstance(value, str):
        return {
            # A legacy string stays on the current Hermes provider.  Keep the
            # historical label for config inspection, but execution/stats use
            # the runtime provider supplied by Hermes.
            "provider": "openrouter",
            "providerExplicit": False,
            "model": value,
            "native": True,
            "routeKind": "native",
            "baseUrl": None,
            "apiKeyEnv": None,
            "apiMode": "chat_completions",
            "timeoutMs": DEFAULT_PROVIDER_TIMEOUT_MS,
            "extraBody": {},
            "direct": False,
        }
    if not isinstance(value, dict):
        raise ValueError("provider entry must be a string or object")
    provider = str(value["provider"]).lower()
    native = value.get("native") is True
    preset = PROVIDER_PRESETS.get(provider, {})
    base_url = None if native else value.get("baseUrl", preset.get("baseUrl"))
    if isinstance(base_url, str):
        base_url = base_url.rstrip("/")
    return {
        "provider": provider,
        "providerExplicit": native,
        "model": value["model"],
        "native": native,
        "routeKind": "native" if native else "direct",
        "baseUrl": base_url,
        "apiKeyEnv": None if native else value.get("apiKeyEnv", preset.get("apiKeyEnv")),
        "apiMode": "chat_completions" if native else value.get("apiMode", "chat_completions"),
        "timeoutMs": value.get("timeoutMs", DEFAULT_PROVIDER_TIMEOUT_MS),
        "extraBody": {} if native else copy.deepcopy(value.get("extraBody") or {}),
        "direct": not native,
    }


def entry_key(entry: dict[str, Any]) -> str:
    """Canonical dedup key; contains no credential values."""
    return json.dumps(
        {key: entry.get(key) for key in ENTRY_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def iter_config_entries(config: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """Yield every configured entry in routing-priority/configuration order."""
    for tier in ("low", "medium", "high"):
        for index, value in enumerate(_items((config.get("globalTiers") or {}).get(tier))):
            yield f"globalTiers.{tier}[{index}]", value
    for task, raw_rule in (config.get("taskRules") or {}).items():
        if is_provider_entry(raw_rule) or not isinstance(raw_rule, dict):
            for index, value in enumerate(_items(raw_rule)):
                suffix = f"[{index}]" if isinstance(raw_rule, list) else ""
                yield f"taskRules.{task}{suffix}", value
            continue
        for key in ("low", "medium", "high", "any", "models"):
            if key not in raw_rule:
                continue
            for index, value in enumerate(_items(raw_rule[key])):
                suffix = f"[{index}]" if isinstance(raw_rule[key], list) else ""
                yield f"taskRules.{task}.{key}{suffix}", value
    if config.get("defaultModel"):
        yield "defaultModel", config["defaultModel"]


def config_warnings(config: dict[str, Any], env: Optional[Mapping[str, str]] = None) -> list[str]:
    """Return warnings without environment values, request data, or credentials."""
    env = os.environ if env is None else env
    warnings: list[str] = []
    seen: dict[str, str] = {}
    for path, raw in iter_config_entries(config):
        entry = normalize_entry(raw)
        key = entry_key(entry)
        if key in seen:
            warnings.append(f"duplicate provider entry at {path} (first at {seen[key]})")
        else:
            seen[key] = path
        key_env = entry.get("apiKeyEnv")
        if key_env and not env.get(key_env):
            warnings.append(f"{path}: ${key_env} is missing; direct calls will fall back")
    return warnings


def entries_for_tier(config: dict[str, Any], tier: str) -> list[dict[str, Any]]:
    values = (config.get("globalTiers") or {}).get(tier) or []
    return [normalize_entry(value) for value in values]


def entries_for_provider(config: dict[str, Any], provider: str) -> list[dict[str, Any]]:
    wanted = provider.lower()
    return [
        normalize_entry(value)
        for _path, value in iter_config_entries(config)
        if normalize_entry(value)["provider"] == wanted
    ]


def unique_provider_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    """One representative per endpoint/auth/mode for diagnostics and probes."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _path, raw in iter_config_entries(config):
        entry = normalize_entry(raw)
        key = json.dumps(
            {key: entry.get(key) for key in ("provider", "baseUrl", "apiKeyEnv", "apiMode")},
            sort_keys=True,
        )
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result
