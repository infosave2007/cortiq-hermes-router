"""cortiq-hermes-router — AllaiGate model routing for Hermes, in-process.

Registers one ``llm_request`` middleware: before every main-loop provider call
it classifies the last user message with AllaiGate, selects a model with the
cortiq-*-router family schema (taskRules -> globalTiers -> defaultModel) and
rewrites ``request["model"]``. Classifier failures degrade to ``defaultModel``
(and then to the current model) — the request never fails because of routing.

Slash command: ``/cortiq-router status | decide <prompt> | on | off``.

Capability ``llm.model_override`` is declared (consent screen at install) and
re-checked live: without the grant the middleware stays pass-through.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import time
from typing import Any, Optional

from .cortiq_router.classify import classify
from .cortiq_router.config import ConfigError, default_config_path, load_config
from .cortiq_router.routing import extract_last_user_text, truncate_text
from .cortiq_router.select import select_model

logger = logging.getLogger(__name__)

CONFIG_RELOAD_SECONDS = 3.0
DECISION_CACHE_TTL = 600.0
DECISION_CACHE_MAX = 128
CAPABILITY_RECHECK_SECONDS = 30.0

_HELP = (
    "cortiq-hermes-router — AllaiGate model routing\n"
    "  /cortiq-router status            config, keys, last routing decision\n"
    "  /cortiq-router decide <prompt>   classify without routing (prints JSON)\n"
    "  /cortiq-router on | off          toggle routing for this profile\n"
    "  /cortiq-router help              this text"
)


def _summarize_tiers(config: dict) -> str:
    tiers = config.get("globalTiers") or {}
    if not tiers:
        return "(no globalTiers configured)"
    parts = []
    for tier in ("low", "medium", "high"):
        models = tiers.get(tier) or []
        if models:
            shown = ", ".join(models[:2]) + (" …" if len(models) > 2 else "")
            parts.append(f"{tier}=[{shown}]")
    return " ".join(parts) or "(no globalTiers configured)"


def register(ctx) -> None:  # noqa: ANN001 - PluginContext, kept untyped for isolated probing
    """Called once by the plugin loader (also by the validate capability probe)."""
    cache: dict[str, Any] = {
        "config_at": 0.0,
        "config": None,
        "config_error": None,
        "decisions": {},
        "capability": (0.0, None),
        "cap_warned": False,
    }

    def _config() -> dict:
        now = time.monotonic()
        if cache["config"] is None or now - cache["config_at"] > CONFIG_RELOAD_SECONDS:
            try:
                cache["config"] = load_config()
                cache["config_error"] = None
            except ConfigError as exc:
                # Invalid config: keep the session's model, warn once per change.
                if cache["config_error"] != str(exc):
                    logger.warning("cortiq-router: %s — routing paused", exc)
                cache["config_error"] = str(exc)
                cache["config"] = {"enabled": False}
            cache["config_at"] = now
        return cache["config"] or {"enabled": False}

    def _capability_granted() -> bool:
        now = time.monotonic()
        stamp, value = cache["capability"]
        if value is None or now - stamp > CAPABILITY_RECHECK_SECONDS:
            try:
                value = bool(ctx.has_capability("llm.model_override"))
            except Exception:
                value = False
            cache["capability"] = (now, value)
        return value

    def _routing_enabled(config: dict) -> bool:
        try:
            override = ctx.get_config("enabled", None)
        except Exception:
            override = None
        if override is None:
            return bool(config.get("enabled", True))
        return bool(override)

    def _remember(selection: dict, original: str) -> None:
        entry = {
            "at": time.time(),
            "task": selection.get("task"),
            "score": selection.get("score"),
            "tier": selection.get("tier"),
            "model": selection.get("model"),
            "candidates": selection.get("candidates"),
            "originalModel": original,
            "fallback": selection.get("fallback"),
        }
        try:
            ctx.state.set("last_decision", entry)
        except Exception as exc:  # state must never break routing
            logger.debug("cortiq-router: cannot persist last_decision: %s", exc)
        cache["last"] = entry

    def _classify_cached(text: str, config: dict) -> Optional[dict]:
        key = hashlib.sha1(
            "|".join([
                text,
                str(config.get("routerProfile")),
                str(config.get("taxonomyId")),
                str(config.get("routerUrl")),
            ]).encode("utf-8", "replace")
        ).hexdigest()
        now = time.monotonic()
        hit = cache["decisions"].get(key)
        if hit and now - hit[0] < DECISION_CACHE_TTL:
            return hit[1]
        decision = classify(text, config)
        if len(cache["decisions"]) >= DECISION_CACHE_MAX:
            cache["decisions"].clear()
        cache["decisions"][key] = (now, decision)
        return decision

    def _on_llm_request(request: Any, **context: Any) -> Optional[dict]:
        """llm_request middleware: may replace the outgoing provider kwargs."""
        if not isinstance(request, dict):
            return None
        config = _config()
        if not _routing_enabled(config):
            return None
        if not _capability_granted():
            if not cache["cap_warned"]:
                cache["cap_warned"] = True
                logger.warning(
                    "cortiq-router: capability llm.model_override not granted — routing skipped. "
                    "Grant it with `hermes plugins enable cortiq-hermes-router` (consent) or "
                    "`hermes plugins capabilities cortiq-hermes-router`."
                )
            return None

        text = truncate_text(extract_last_user_text(request.get("messages")),
                             int(config.get("maxChars", 12000)))
        if not text.strip():
            return None

        decision = _classify_cached(text, config)
        selection = select_model(config, decision)
        chosen = selection.get("model") or ""
        original = request.get("model") if isinstance(request.get("model"), str) else ""
        _remember(selection, original)
        if not chosen or chosen == original:
            if config.get("echoRouting"):
                logger.warning(
                    "cortiq-router: task=%s score=%s tier=%s model=%s (unchanged)",
                    selection.get("task"), selection.get("score"),
                    selection.get("tier"), original or "-",
                )
            return None

        reason = "task={task};score={score};tier={tier};model={model}".format(
            task=selection.get("task"),
            score="na" if selection.get("score") is None else f"{selection['score']:.4f}",
            tier=selection.get("tier") or "na",
            model=chosen,
        )
        if config.get("echoRouting"):
            logger.warning("cortiq-router: %s (was %s)", reason, original or "-")
        new_request = dict(request)
        new_request["model"] = chosen
        return {"request": new_request, "source": "cortiq-hermes-router", "reason": reason}

    def _status_text() -> str:
        config = _config()
        try:
            override = ctx.get_config("enabled", None)
        except Exception:
            override = None
        key_name = config.get("apiKeyEnv", "CORTIQ_ROUTER_KEY")
        import os
        key_set = bool(os.environ.get(key_name))
        lines = [
            "cortiq-hermes-router status",
            f"  routing        {'ON' if _routing_enabled(config) else 'OFF'}"
            f"{' (override)' if override is not None else ''} — config.enabled={config.get('enabled')}",
            f"  config         {default_config_path()}",
            f"  classifier     {config.get('routerUrl')} (taxonomy {config.get('taxonomyId')}), "
            f"profile {config.get('routerProfile')}, timeout {config.get('timeoutMs')}ms",
            f"  key            ${key_name}: {'set' if key_set else 'MISSING (fallback chain active)'}",
            f"  tiers          {_summarize_tiers(config)}",
            f"  defaultModel   {config.get('defaultModel') or '(unset)'}",
            f"  capability     llm.model_override: "
            f"{'granted' if _capability_granted() else 'NOT granted — routing skipped'}",
        ]
        last = None
        try:
            last = ctx.state.get("last_decision", None)
        except Exception:
            last = cache.get("last")
        if last:
            lines.append(f"  last decision  task={last.get('task')} score={last.get('score')} "
                         f"tier={last.get('tier')} model={last.get('model')} "
                         f"(was {last.get('originalModel') or '-'})")
        else:
            lines.append("  last decision  (none yet)")
        return "\n".join(lines)

    def _decide_text(prompt: str) -> str:
        config = _config()
        decision = classify(prompt, config)
        selection = select_model(config, decision)
        payload = {
            "task": selection.get("task"),
            "score": selection.get("score"),
            "tier": selection.get("tier"),
            "model": selection.get("model"),
            "candidates": selection.get("candidates"),
            "fallback": selection.get("fallback"),
            "classifier": "ok" if decision else "unavailable",
            "source": decision.get("source") if decision else None,
            "requestId": decision.get("requestId") if decision else None,
            "profile": config.get("routerProfile"),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _handle(raw_args: str) -> str:
        try:
            argv = shlex.split(raw_args or "")
        except ValueError:
            argv = (raw_args or "").split()
        sub = (argv[0].lower() if argv else "status")
        if sub in ("status",):
            return _status_text()
        if sub == "decide":
            prompt = " ".join(argv[1:]).strip()
            if not prompt:
                return 'usage: /cortiq-router decide "<prompt>"'
            return _decide_text(prompt)
        if sub in ("on", "off"):
            ctx.set_config("enabled", sub == "on")
            return f"cortiq-hermes-router: routing {'ON' if sub == 'on' else 'OFF'} for this profile."
        if sub in ("help", "-h", "--help"):
            return _HELP
        return f"unknown subcommand: {sub}\n\n{_HELP}"

    ctx.register_middleware("llm_request", _on_llm_request)
    ctx.register_command("cortiq-router", handler=_handle,
                         description="AllaiGate model routing: status, decide, on, off.",
                         args_hint="[status|decide <prompt>|on|off]")