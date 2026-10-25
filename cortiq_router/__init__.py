"""cortiq-hermes-router — AllaiGate model routing library for the Hermes plugin."""

from .config import ConfigError, DEFAULTS, load_config, parse_config
from .classify import build_route_body, classify, parse_route_decision
from .routing import extract_last_user_text, truncate_text
from .select import candidates_for, parse_task_rule, resolve_tier, select_model

__version__ = "0.2.0"

__all__ = [
    "ConfigError", "DEFAULTS", "load_config", "parse_config",
    "build_route_body", "classify", "parse_route_decision",
    "extract_last_user_text", "truncate_text",
    "candidates_for", "parse_task_rule", "resolve_tier", "select_model",
    "__version__",
]