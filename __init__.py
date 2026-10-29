"""cortiq-hermes-router — AllaiGate model routing for Hermes, in-process.

Two middlewares:

* ``llm_request`` classifies the last user message with AllaiGate, selects an
  endpoint with the cortiq-*-router schema (taskRules -> globalTiers ->
  defaultModel) and rewrites ``request["model"]``. Classification runs only on
  the first call of a turn (``api_call_count == 1``); later tool-loop steps skip
  it and go to the cheapest configured model — see "Cost control" in the README.
* ``llm_execution`` runs object entries (``{"provider", "model", "baseUrl", ...}``)
  against their own endpoint: Ollama, LM Studio, vLLM, llama.cpp, CMF/cortiq or a
  cloud provider. Any failure falls open to the model Hermes was going to use and
  is counted by reason in the statistics.

String entries keep using Hermes' own OpenRouter client, exactly as before.
Classifier failures degrade to ``defaultModel`` (and then to the current model);
routing never fails a turn.

Slash command: ``/cortiq-router status | stats | providers | test | decide | on | off``.

Capabilities: ``llm.model_override`` (rewrite the model) and
``llm.provider_override`` (direct calls to endpoints other than Hermes' own).
Both are re-checked live; without a grant the affected path is skipped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from .cortiq_router.classify import classify, classify_detailed
from .cortiq_router.config import ConfigError, default_config_path, load_config
from .cortiq_router.execution import (
    ExecutionError, call_endpoint, native_text_response, probe_models, responses_to_chat_request, run_probe,
)
from .cortiq_router.providers import (
    config_warnings,
    entries_for_provider,
    entries_for_tier,
    iter_config_entries,
    normalize_entry,
    unique_provider_entries,
)
from .cortiq_router.routing import extract_last_user_text, truncate_text
from .cortiq_router.select import select_model
from .cortiq_router.stats import StatsAggregator, _table, format_recent, format_stats

logger = logging.getLogger(__name__)

CONFIG_RELOAD_SECONDS = 3.0
DECISION_CACHE_TTL = 600.0
DECISION_CACHE_MAX = 128
CAPABILITY_RECHECK_SECONDS = 30.0
PENDING_LIMIT = 64
PROBE_TIMEOUT_SECONDS = 3.0
PROBE_PROMPT = "Reply with one word: pong"
MODEL_CAPABILITY = "llm.model_override"
PROVIDER_CAPABILITY = "llm.provider_override"
TIER_NAMES = ("low", "medium", "high")

_HELP = (
    "cortiq-hermes-router — AllaiGate model routing\n"
    "  /cortiq-router status                  config, keys, endpoints, last routing decision\n"
    "  /cortiq-router providers               configured endpoints: GET {baseUrl}/models (3s, no completions, keys hidden)\n"
    "  /cortiq-router test <tier|provider>    one cheap prompt through that endpoint (explicit only)\n"
    "  /cortiq-router stats [today|week|all]  persistent routing statistics\n"
    "  /cortiq-router stats recent [N]        newest N decisions (default 10, max 200)\n"
    "  /cortiq-router stats --json            full machine-readable aggregate\n"
    "  /cortiq-router stats reset [yes]       clear statistics (requires yes)\n"
    "  /cortiq-router clean [yes]             same as stats reset: wipe statistics (requires yes)\n"
    "  /cortiq-router decide <prompt>         classify without routing (prints JSON)\n"
    "  /cortiq-router on | off                toggle routing for this profile\n"
    "  /cortiq-router help                    this text"
)


def _cheap_selection(config: dict) -> dict[str, Any]:
    """The tool-loop continuation choice: first ``globalTiers.low`` entry, else ``defaultModel``.

    Deliberately ignores the classifier and the task rules: a continuation step
    only needs a model that can keep reading a large tool loop cheaply.
    """
    tiers = config.get("globalTiers") or {}
    values = tiers.get("low") or []
    raw = values[0] if values else (config.get("defaultModel") or "")
    entry = normalize_entry(raw) if raw else None
    model = entry["model"] if entry else ""
    return {
        "task": None,
        "score": None,
        "tier": "low" if values else None,
        "model": model,
        "entry": entry,
        "entries": [entry] if entry else [],
        "candidates": [model] if model else [],
        "fallback": not bool(values),
        "taskRuleMatched": False,
        "step_continuation": True,
    }


def _label(value: Any) -> str:
    entry = normalize_entry(value)
    return f"{entry['provider']}:{entry['model']}" if entry["direct"] else str(entry["model"])


def _summarize_tiers(config: dict) -> str:
    tiers = config.get("globalTiers") or {}
    if not tiers:
        return "(no globalTiers configured)"
    parts = []
    for tier in TIER_NAMES:
        values = tiers.get(tier) or []
        if values:
            shown = ", ".join(_label(value) for value in values[:2]) + (" …" if len(values) > 2 else "")
            parts.append(f"{tier}=[{shown}]")
    return " ".join(parts) or "(no globalTiers configured)"


def _key_status(entry: dict, env: Any) -> str:
    name = entry.get("apiKeyEnv")
    if not name:
        return "not required"
    return f"${name} set" if env.get(name) else f"${name} MISSING (fallback active)"


def register(ctx) -> None:  # noqa: ANN001 - PluginContext, kept untyped for isolated probing
    """Called once by the plugin loader (also by the validate capability probe)."""
    statistics = StatsAggregator(getattr(ctx, "state", None))
    cache: dict[str, Any] = {
        "config_at": 0.0,
        "config": None,
        "config_error": None,
        "config_warnings": None,
        "decisions": {},
        "capabilities": {},
        "cap_warned": set(),
        "fallback_warned": set(),
    }
    pending: OrderedDict[str, dict[str, Any]] = OrderedDict()
    pending_lock = threading.Lock()

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
            if cache["config_error"] is None:
                warnings = config_warnings(cache["config"])
                if warnings != cache["config_warnings"]:
                    cache["config_warnings"] = warnings
                    for warning in warnings:
                        logger.warning("cortiq-router: %s", warning)
        return cache["config"] or {"enabled": False}

    def _capability_granted(name: str) -> bool:
        now = time.monotonic()
        stamp, value = cache["capabilities"].get(name, (0.0, None))
        if value is None or now - stamp > CAPABILITY_RECHECK_SECONDS:
            try:
                value = bool(ctx.has_capability(name))
            except Exception:
                value = False
            cache["capabilities"][name] = (now, value)
        return value

    def _warn_capability(name: str, message: str) -> None:
        if name not in cache["cap_warned"]:
            cache["cap_warned"].add(name)
            logger.warning("cortiq-router: %s", message)

    def _routing_enabled(config: dict) -> bool:
        try:
            override = ctx.get_config("enabled", None)
        except Exception:
            override = None
        if override is None:
            return bool(config.get("enabled", True))
        return bool(override)

    def _remember(selection: dict, original: str, *, status: str,
                  classifier_latency_ms: Optional[float], at: float) -> None:
        # ``step_continuation`` marks a tool-loop step that skipped classification.
        chosen = selection.get("model") or original or None
        entry = selection.get("entry") or {}
        stored = statistics.record({
            "task": selection.get("task"),
            "score": selection.get("score"),
            "tier": selection.get("tier"),
            "model": chosen,
            "originalModel": original or None,
            "provider": selection.get("actual_provider"),
            "route_kind": selection.get("route_kind") or "unchanged",
            "fallback": selection.get("fallback", False),
            "status": status,
            "step_continuation": bool(selection.get("step_continuation")),
            "classifier_latency_ms": classifier_latency_ms,
        }, at=at)
        last = dict(stored)
        last["at"] = at
        last["candidates"] = selection.get("candidates")
        try:
            ctx.state.set("last_decision", last)
        except Exception as exc:  # state must never break routing
            logger.debug("cortiq-router: cannot persist last_decision: %s", exc)
        cache["last"] = last

    def _remember_skipped(original: str, status: str, *, fallback: bool, at: float) -> None:
        _remember({
            "task": "unclassified",
            "score": None,
            "tier": None,
            "model": original,
            "candidates": [],
            "step_continuation": False,
            "fallback": fallback,
        }, original, status=status, classifier_latency_ms=None, at=at)

    def _classify_cached(text: str, config: dict, turn_id: Any = None) -> tuple[Optional[dict], dict]:
        # turn_id pins one turn's decision: retries reuse it, a new turn re-classifies.
        key = hashlib.sha1(
            "|".join([
                text,
                str(turn_id),
                str(config.get("routerProfile")),
                str(config.get("taxonomyId")),
                str(config.get("routerUrl")),
            ]).encode("utf-8", "replace")
        ).hexdigest()
        now = time.monotonic()
        hit = cache["decisions"].get(key)
        if hit and now - hit[0] < DECISION_CACHE_TTL:
            metadata = dict(hit[2])
            metadata["classifier_latency_ms"] = None
            return hit[1], metadata
        decision, metadata = classify_detailed(text, config)
        if len(cache["decisions"]) >= DECISION_CACHE_MAX:
            cache["decisions"].clear()
        cache["decisions"][key] = (now, decision, metadata)
        return decision, metadata

    def _pending_put(key: str, value: dict[str, Any]) -> None:
        with pending_lock:
            pending[key] = value
            while len(pending) > PENDING_LIMIT:
                pending.popitem(last=False)

    def _pending_take(key: Any) -> Optional[dict[str, Any]]:
        if not key:
            return None
        with pending_lock:
            return pending.pop(str(key), None)

    def _on_llm_request(request: Any, **context: Any) -> Optional[dict]:
        """llm_request middleware: may replace the outgoing provider kwargs."""
        started_at = time.time()
        original = ""
        if isinstance(request, dict) and isinstance(request.get("model"), str):
            original = request["model"]
        if not isinstance(request, dict):
            _remember_skipped(original, "disabled", fallback=False, at=started_at)
            return None
        config = _config()
        if cache["config_error"] is not None:
            _remember_skipped(original, "invalid_config", fallback=True, at=started_at)
            return None
        if not _routing_enabled(config):
            _remember_skipped(original, "disabled", fallback=False, at=started_at)
            return None
        if not _capability_granted(MODEL_CAPABILITY):
            _warn_capability(MODEL_CAPABILITY,
                             "capability llm.model_override not granted — routing skipped. Grant it with "
                             "`hermes plugins enable cortiq-hermes-router` (consent) or "
                             "`hermes plugins capabilities cortiq-hermes-router`.")
            _remember_skipped(original, "disabled", fallback=False, at=started_at)
            return None

        text = truncate_text(extract_last_user_text(request),
                             int(config.get("maxChars", 12000)))
        if not text.strip():
            _remember_skipped(original, "disabled", fallback=False, at=started_at)
            return None

        # Cost control: classify only the first call of a turn; every later tool
        # step of the same turn is routed to the cheapest configured model.
        api_call_count = context.get("api_call_count")
        if not isinstance(api_call_count, int) or isinstance(api_call_count, bool):
            api_call_count = 1
        step_continuation = api_call_count > 1
        if step_continuation:
            selection = _cheap_selection(config)
            entry = selection.get("entry") or {}
            status, latency = "step_continuation", None
            decision = None
        else:
            decision, metadata = _classify_cached(text, config, context.get("turn_id"))
            selection = select_model(config, decision)
            selection["step_continuation"] = False
            entry = selection.get("entry") or {}
            status = metadata.get("status", "ok" if decision else "classifier_error")
            latency = metadata.get("classifier_latency_ms")

        runtime_provider = str(context.get("provider") or "unknown")
        if entry.get("direct"):
            selection["actual_provider"] = entry.get("provider") or "unknown"
            selection["route_kind"] = "direct"
        elif entry.get("native"):
            selection["actual_provider"] = (entry.get("provider") if entry.get("providerExplicit")
                                            else runtime_provider)
            selection["route_kind"] = "native" if selection.get("model") != original else "unchanged"
        else:
            selection["actual_provider"] = runtime_provider
            selection["route_kind"] = "unchanged"

        requires_execution = bool(entry.get("direct") or (entry.get("native") and entry.get("providerExplicit")))
        if requires_execution:
            # Endpoint and explicit-native calls are correlated with llm_execution
            # by api_request_id; the model is only rewritten when that pairing exists.
            request_key = context.get("api_request_id")
            if not request_key:
                _remember(dict(selection, model=original, entry=None, route_kind="unchanged",
                               actual_provider=runtime_provider), original,
                          status="no_request_id", classifier_latency_ms=latency, at=started_at)
                return None
            if not _capability_granted(PROVIDER_CAPABILITY):
                _warn_capability(PROVIDER_CAPABILITY,
                                 "capability llm.provider_override not granted — direct endpoints skipped. "
                                 "Grant it with `hermes plugins capabilities cortiq-hermes-router`.")
                _remember(dict(selection, model=original, entry=None, route_kind="unchanged",
                               actual_provider=runtime_provider), original,
                          status="provider_override_not_granted", classifier_latency_ms=latency,
                          at=started_at)
                return None
            _pending_put(str(request_key), {"entry": entry, "originalModel": original,
                                             "selection": selection, "status": status,
                                             "latency": latency, "at": started_at})

        if not requires_execution:
            _remember(selection, original, status=status, classifier_latency_ms=latency, at=started_at)
        chosen = selection.get("model") or ""
        tier = selection.get("tier")
        effort = (config.get("tierReasoning") or {}).get(tier)
        new_request = dict(request)
        if chosen:
            new_request["model"] = chosen
        if effort:
            # This middleware runs after Hermes has built provider kwargs. The
            # OpenAI/Codex Responses wire accepts ``reasoning.effort``; it does
            # not accept Hermes' internal ``enabled`` flag.
            new_request["reasoning"] = {"effort": effort}
        if new_request == request:
            if config.get("echoRouting"):
                logger.warning(
                    "cortiq-router: task=%s score=%s tier=%s model=%s (unchanged)%s",
                    selection.get("task"), selection.get("score"),
                    selection.get("tier"), original or "-",
                    " [tool step]" if step_continuation else "",
                )
            return None

        reason = "task={task};score={score};tier={tier};provider={provider};model={model}{effort}{cont}".format(
            task=selection.get("task") or ("continuation" if step_continuation else "na"),
            score="na" if selection.get("score") is None else f"{selection['score']:.4f}",
            tier=tier or "na",
            provider=entry.get("provider") or "hermes",
            model=chosen,
            effort=(";reasoning=" + effort) if effort else "",
            cont=";step_continuation" if step_continuation else "",
        )
        if config.get("echoRouting"):
            logger.warning("cortiq-router: %s (was %s)", reason, original or "-")
        return {"request": new_request, "source": "cortiq-hermes-router", "reason": reason}

    def _on_llm_execution(request: Any, next_call: Any, **context: Any) -> Any:
        """llm_execution middleware: run object entries directly; fail open on any error."""
        record = _pending_take(context.get("api_request_id"))
        if record is None or not isinstance(request, dict):
            return next_call(request)

        entry = record["entry"]
        provider = str(entry.get("provider") or "unknown")
        selection = dict(record.get("selection") or {})
        original_model = record.get("originalModel") or request.get("model")
        fallback_request = dict(request)
        fallback_request["model"] = original_model

        def remember_success(updated: Optional[dict] = None) -> None:
            _remember(updated or selection, str(original_model or ""), status=record.get("status", "ok"),
                      classifier_latency_ms=record.get("latency"), at=record.get("at", time.time()))

        def fall_open(reason: str) -> Any:
            fallback_selection = dict(selection, model=original_model, fallback=True,
                                      actual_provider=str(context.get("provider") or "unknown"),
                                      route_kind="fallback")
            _remember(fallback_selection, str(original_model or ""), status="execution_fallback",
                      classifier_latency_ms=record.get("latency"), at=record.get("at", time.time()))
            statistics.record_execution_fallback(provider, reason, at=time.time())
            key = (provider, reason)
            if key not in cache["fallback_warned"]:
                cache["fallback_warned"].add(key)
                logger.warning("cortiq-router: %s endpoint failed (%s); using the Hermes model instead",
                               provider, reason)
            else:
                logger.debug("cortiq-router: %s fell open (%s)", provider, reason)
            return next_call(fallback_request)

        if not _capability_granted(PROVIDER_CAPABILITY):
            return fall_open("provider_override_not_granted")
        try:
            if entry.get("native"):
                if request.get("tools"):
                    raise ExecutionError("native_tools_unsupported", "native provider override cannot run tool turns")
                facade = getattr(ctx, "llm", None)
                complete = getattr(facade, "complete", None)
                if not callable(complete):
                    raise ExecutionError("native_unavailable", "Hermes plugin LLM facade is unavailable")
                native_request = responses_to_chat_request(request) if "input" in request else dict(request)
                native = complete(
                    native_request.get("messages") or [], provider=entry["provider"], model=entry["model"],
                    temperature=native_request.get("temperature"), max_tokens=native_request.get("max_tokens"),
                    timeout=max(float(entry.get("timeoutMs") or 60000) / 1000.0, 0.001),
                    purpose="cortiq-router-native-route",
                )
                native_selection = dict(selection,
                                        actual_provider=str(getattr(native, "provider", None) or provider),
                                        model=str(getattr(native, "model", None) or entry["model"]),
                                        route_kind="native")
                reply = native_text_response(text=getattr(native, "text", None), model=getattr(native, "model", None),
                                             usage=getattr(native, "usage", None), request=request)
                remember_success(native_selection)
                return reply
            reply = call_endpoint(entry, request)
            remember_success()
            return reply
        except ExecutionError as exc:
            return fall_open(exc.reason)
        except Exception:  # routing must never break a turn
            logger.debug("cortiq-router: unexpected endpoint error", exc_info=True)
            return fall_open("native_execution_error" if entry.get("native") else "internal_error")

    def _status_text() -> str:
        config = _config()
        try:
            override = ctx.get_config("enabled", None)
        except Exception:
            override = None
        key_name = config.get("apiKeyEnv", "CORTIQ_ROUTER_KEY")
        providers = sorted({entry["provider"] for entry in unique_provider_entries(config) if entry["direct"]})
        lines = [
            "cortiq-hermes-router status",
            f"  routing        {'ON' if _routing_enabled(config) else 'OFF'}"
            f"{' (override)' if override is not None else ''} — config.enabled={config.get('enabled')}",
            f"  config         {default_config_path()}",
            f"  classifier     {config.get('routerUrl')} (taxonomy {config.get('taxonomyId')}), "
            f"profile {config.get('routerProfile')}, timeout {config.get('timeoutMs')}ms",
            f"  key            ${key_name}: "
            f"{'set' if os.environ.get(key_name) else 'MISSING (fallback chain active)'}",
            f"  tiers          {_summarize_tiers(config)}",
            f"  defaultModel   {_label(config['defaultModel']) if config.get('defaultModel') else '(unset)'}",
            "  endpoints      "
            + (", ".join(providers) if providers else "(none: every entry uses Hermes' OpenRouter client)"),
            f"  capability     llm.model_override: "
            f"{'granted' if _capability_granted(MODEL_CAPABILITY) else 'NOT granted — routing skipped'}; "
            f"llm.provider_override: "
            f"{'granted' if _capability_granted(PROVIDER_CAPABILITY) else 'NOT granted — direct endpoints skipped'}",
        ]
        last = None
        try:
            last = ctx.state.get("last_decision", None)
        except Exception:
            last = cache.get("last")
        if last:
            lines.append(f"  last decision  task={last.get('task')} score={last.get('score')} "
                         f"tier={last.get('tier')} model={last.get('model')} "
                         f"provider={last.get('provider') or 'hermes'} "
                         f"(was {last.get('originalModel') or '-'})")
        else:
            lines.append("  last decision  (none yet)")
        lines.append("  statistics     /cortiq-router stats (today/week/all, recent, --json)")
        lines.append("  endpoints      /cortiq-router providers (probe), /cortiq-router test <tier|provider>")
        return "\n".join(lines)

    def _providers_text() -> str:
        config = _config()
        env = os.environ
        direct = [entry for entry in unique_provider_entries(config) if entry["direct"]]
        managed = sum(1 for _path, value in iter_config_entries(config) if isinstance(value, str))
        if not direct:
            return ("cortiq-hermes-router providers\n"
                    "(no direct endpoints configured; every entry uses Hermes' OpenRouter client)")
        rows = []
        for entry in direct:
            probe = probe_models(entry, env=env, timeout=PROBE_TIMEOUT_SECONDS)
            if probe["ok"]:
                listed = "model listed" if probe["modelListed"] else "model NOT listed"
                state = f"HTTP 200, {probe['models']} models, {listed}"
            else:
                state = f"failed: {probe['reason']}"
            rows.append([
                entry["provider"], entry["baseUrl"], entry["apiMode"],
                _key_status(entry, env), state, f"{probe['latencyMs']:.0f}ms",
            ])
        return (
            "cortiq-hermes-router providers (GET {baseUrl}/models; no completions; keys never shown)\n"
            + _table(["Provider", "Base URL", "API mode", "Key", "Probe", "Time"], rows)
            + f"\nHermes-managed string entries (OpenRouter): {managed}"
        )

    def _test_text(args: list[str]) -> str:
        usage = "usage: /cortiq-router test <low|medium|high|provider-name>"
        if len(args) != 1:
            return usage
        target = args[0].lower()
        config = _config()
        if target in TIER_NAMES:
            candidates = entries_for_tier(config, target)
            scope = f"tier {target}"
        else:
            candidates = entries_for_provider(config, target)
            scope = f"provider {target}"
        if not candidates:
            return f"cortiq-hermes-router test: no entries configured for {scope}"
        direct = [entry for entry in candidates if entry["direct"]]
        if not direct:
            return (f"cortiq-hermes-router test: {scope} has only Hermes-managed string entries; "
                    "there is no direct endpoint to test.")
        entry = direct[0]
        result = run_probe(entry, PROBE_PROMPT, env=os.environ)
        lines = [
            f"cortiq-hermes-router test — {scope}",
            f"  provider   {entry['provider']}",
            f"  endpoint   {entry['baseUrl']}",
            f"  model      {entry['model']}",
            f"  result     {'ok' if result['ok'] else 'failed: ' + str(result['reason'])}",
            f"  latency    {result['latencyMs']:.0f}ms",
        ]
        if result["ok"]:
            lines.append(f"  reply      {result['reply'] or '(empty)'}")
        lines.append("  one prompt, max_tokens=8; runs only on this explicit command")
        return "\n".join(lines)

    def _stats_text(args: list[str]) -> str:
        usage = ("usage: /cortiq-router stats [today|week|all] | recent [N] | "
                 "--json | reset yes")
        if not args:
            return format_stats(statistics)
        if args == ["--json"]:
            return json.dumps(statistics.snapshot(), ensure_ascii=False, indent=2)
        if len(args) == 1 and args[0] in ("today", "week", "all"):
            return format_stats(statistics, args[0])
        if args[0] == "recent" and len(args) <= 2:
            try:
                limit = 10 if len(args) == 1 else int(args[1])
                return format_recent(statistics.recent(limit))
            except (ValueError, TypeError):
                return usage
        if args[0] == "reset":
            if args == ["reset", "yes"]:
                statistics.reset()
                return "cortiq-hermes-router: statistics reset; last_decision preserved."
            if len(args) == 1:
                return ("This permanently clears totals, daily history and recent decisions.\n"
                        "To confirm: /cortiq-router stats reset yes")
        return usage

    def _decide_text(prompt: str) -> str:
        config = _config()
        decision = classify(prompt, config)
        selection = select_model(config, decision)
        entry = selection.get("entry") or {}
        payload = {
            "task": selection.get("task"),
            "score": selection.get("score"),
            "tier": selection.get("tier"),
            "model": selection.get("model"),
            "provider": entry.get("provider"),
            "endpoint": entry.get("baseUrl") if entry.get("direct") else None,
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
        if sub == "providers":
            return _providers_text()
        if sub == "test":
            return _test_text(argv[1:])
        if sub == "stats":
            return _stats_text([arg.lower() for arg in argv[1:]])
        if sub == "decide":
            prompt = " ".join(argv[1:]).strip()
            if not prompt:
                return 'usage: /cortiq-router decide "<prompt>"'
            return _decide_text(prompt)
        if sub == "clean":
            if argv[1:] == ["yes"]:
                statistics.reset()
                return "cortiq-hermes-router: statistics cleaned; last_decision preserved."
            return ("This permanently clears totals, daily history and recent decisions.\n"
                    "To confirm: /cortiq-router clean yes")
        if sub in ("on", "off"):
            ctx.set_config("enabled", sub == "on")
            return f"cortiq-hermes-router: routing {'ON' if sub == 'on' else 'OFF'} for this profile."
        if sub in ("help", "-h", "--help"):
            return _HELP
        return f"unknown subcommand: {sub}\n\n{_HELP}"

    ctx.register_middleware("llm_request", _on_llm_request)
    ctx.register_middleware("llm_execution", _on_llm_execution)
    ctx.register_command(
        "cortiq-router",
        handler=_handle,
        description="AllaiGate routing: status, providers, test, statistics, clean, decide, on, off.",
        args_hint="[status|providers|test <tier|provider>|stats [today|week|all|recent [N]|--json|reset yes]|"
                  "clean yes|decide <prompt>|on|off]",
    )
