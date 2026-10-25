"""Configuration for cortiq-hermes-router — the cortiq-*-router family schema.

Validation is strict: an unknown setting is an error, not a warning.
Config file: ``~/.config/cortiq-hermes-router/config.json`` (override with
``CORTIQ_ROUTER_CONFIG``). The same file is understood by the optional
standalone proxy in ``standalone/``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

PROFILES: tuple[str, ...] = ("balanced", "cost-saver", "quality-first")
TIERS: tuple[str, ...] = ("low", "medium", "high")

FIELDS = frozenset({
    "enabled", "apiKeyEnv", "routerUrl", "taxonomyId", "timeoutMs", "maxChars",
    "routerProfile", "complexityBands", "globalTiers", "taskRules",
    "defaultModel", "echoRouting",
})

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MODEL_ID = re.compile(r"^[^/\s]+/\S+$")

#: Defaults — the family defaults table (owner's source of truth).
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "apiKeyEnv": "CORTIQ_ROUTER_KEY",
    "routerUrl": "https://router.allaigate.com",
    "taxonomyId": "data-assistant",
    "timeoutMs": 15000,
    "maxChars": 12000,
    "routerProfile": "balanced",
    "complexityBands": None,
    "globalTiers": {},
    "taskRules": {},
    "defaultModel": "",
    "echoRouting": False,
}


class ConfigError(ValueError):
    """Invalid configuration; routing must degrade, never crash."""


def _invalid(field: str) -> None:
    raise ConfigError(f"Invalid cortiq-hermes-router configuration field: {field}")


def _is_object(value: Any) -> bool:
    return isinstance(value, dict)


def _ensure_model(value: Any, path_: str) -> str:
    if not isinstance(value, str) or not _MODEL_ID.match(value):
        _invalid(path_)
    return value


def _ensure_models(value: Any, path_: str, single: bool = False) -> None:
    if single and isinstance(value, str):
        _ensure_model(value, path_)
        return
    if not isinstance(value, list):
        _invalid(path_)
    for item in value:
        _ensure_model(item, path_)


def _ensure_router_url(value: Any, field: str) -> str:
    from urllib.parse import urlparse
    if not isinstance(value, str):
        _invalid(field)
    try:
        url = urlparse(value)
    except ValueError:
        _invalid(field)
        raise
    if url.scheme not in ("http", "https") or url.username or url.password or url.query or url.fragment or not url.netloc:
        _invalid(field)
    if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
        _invalid(f"{field} (HTTP requires loopback)")
    return value.rstrip("/")


def parse_config(raw: Any) -> dict:
    """Validate a raw config object and return the normalized config."""
    if not _is_object(raw):
        _invalid("root")
    for field in raw:
        if field not in FIELDS:
            _invalid("unknown setting")

    for field in ("enabled", "echoRouting"):
        if raw.get(field) is not None and not isinstance(raw[field], bool):
            _invalid(field)
    for field in ("apiKeyEnv", "taxonomyId"):
        value = raw.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            _invalid(field)
    if raw.get("apiKeyEnv") is not None and not _ENV_NAME.match(str(raw["apiKeyEnv"])):
        _invalid("apiKeyEnv")
    for field, maximum in (("timeoutMs", 120000), ("maxChars", 1000000)):
        value = raw.get(field)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                  or value < 1 or value > maximum):
            _invalid(field)
    profile = raw.get("routerProfile")
    if profile is not None and (not isinstance(profile, str) or profile not in PROFILES):
        _invalid("routerProfile")

    router_url = "https://router.allaigate.com"
    if raw.get("routerUrl") is not None:
        router_url = _ensure_router_url(raw["routerUrl"], "routerUrl")

    bands = raw.get("complexityBands")
    if bands is not None:
        if not _is_object(bands) or any(k not in ("low", "medium") for k in bands) \
                or not isinstance(bands.get("low"), (int, float)) or isinstance(bands.get("low"), bool) \
                or not isinstance(bands.get("medium"), (int, float)) or isinstance(bands.get("medium"), bool) \
                or not (0 <= bands["low"] < bands["medium"] <= 1):
            _invalid("complexityBands")

    if raw.get("globalTiers") is not None:
        if not _is_object(raw["globalTiers"]):
            _invalid("globalTiers")
        for tier, value in raw["globalTiers"].items():
            if tier not in TIERS:
                _invalid("globalTiers")
            _ensure_models(value, "globalTiers")

    if raw.get("taskRules") is not None:
        if not _is_object(raw["taskRules"]):
            _invalid("taskRules")
        for value in raw["taskRules"].values():
            if _is_object(value):
                for key, models in value.items():
                    if key not in (*TIERS, "any", "models"):
                        _invalid("taskRules")
                    _ensure_models(models, "taskRules", single=True)
            else:
                _ensure_models(value, "taskRules", single=True)

    default_model = raw.get("defaultModel")
    if default_model not in (None, ""):
        _ensure_model(default_model, "defaultModel")

    config = dict(DEFAULTS)
    config.update({k: v for k, v in raw.items()})
    config["routerUrl"] = router_url
    return config


def default_config_path(env: Optional[dict] = None) -> Path:
    env = os.environ if env is None else env
    if env.get("CORTIQ_ROUTER_CONFIG"):
        return Path(env["CORTIQ_ROUTER_CONFIG"]).expanduser()
    return Path.home() / ".config" / "cortiq-hermes-router" / "config.json"


def load_config(path: Optional[str] = None, env: Optional[dict] = None) -> dict:
    """Load and validate the config.

    An explicitly selected missing file (``path`` or ``CORTIQ_ROUTER_CONFIG``)
    is an error; a missing default file means empty routing defaults.
    """
    env = os.environ if env is None else env
    explicit = path is not None or bool(env.get("CORTIQ_ROUTER_CONFIG"))
    target = Path(path).expanduser() if path else default_config_path(env)
    try:
        contents = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"Cannot read cortiq-hermes-router configuration file: {target}") from None
        return parse_config({})
    except OSError as exc:
        raise ConfigError(f"Cannot read cortiq-hermes-router configuration file: {target}: {exc}") from exc
    try:
        raw = json.loads(contents)
    except ValueError:
        raise ConfigError(f"Cortiq configuration must be valid JSON: {target}") from None
    return parse_config(raw)