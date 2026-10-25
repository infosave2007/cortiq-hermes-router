"""Pure model-selection logic — the cortiq-*-router family routing table, ported to Python.

Priority for a classified request:
  1. task rule models FOR the resolved complexity tier;
  2. tier == high -> the global ``high`` chain first, then the task's ``any``
     models (escalation must not be undercut); tier low/medium -> the task's
     ``any`` models first, then the global tier chain;
  3. ``defaultModel`` as the last candidate.

Classifier failure -> ``defaultModel`` directly (``fallback`` flag set).

No I/O: every step is a pure function of the config and the classifier
decision, so the whole priority chain is unit-testable.
"""

from __future__ import annotations

from typing import Any, Optional

TIERS: tuple[str, ...] = ("low", "medium", "high")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def parse_task_rule(value: Any) -> dict[str, Any]:
    """Normalize a task rule value to ``{"any": [...], "per": {tier: [...]}}``.

    Accepts a plain string, an array, or a tier object whose ``any`` key may
    be spelled ``models`` (family alias).
    """
    if value is None:
        return {"any": [], "per": {}}
    if isinstance(value, str):
        return {"any": [value], "per": {}}
    if isinstance(value, list):
        return {"any": [str(v) for v in value], "per": {}}
    if isinstance(value, dict):
        any_models = _as_list(value.get("any", value.get("models")))
        per = {}
        for tier in TIERS:
            if tier in value:
                per[tier] = _as_list(value[tier])
        return {"any": any_models, "per": per}
    return {"any": [str(value)], "per": {}}


def resolve_tier(score: float, router_tier: str, bands: Optional[dict] = None) -> str:
    """Effective tier: without custom bands the router's tier stands; bands are
    inclusive on the upper bound (``score <= low`` -> low, ``score <= medium`` -> medium)."""
    if not bands:
        return router_tier
    if score <= bands["low"]:
        return "low"
    if score <= bands["medium"]:
        return "medium"
    return "high"


def candidates_for(config: dict, task: str, tier: str) -> tuple[list[str], bool]:
    """Ordered candidate chain for ``(task, tier)``; deduplicated, empties dropped."""
    out: list[str] = []

    def push(model_id: str) -> None:
        if model_id and model_id not in out:
            out.append(model_id)

    global_tiers = config.get("globalTiers") or {}
    default_model = config.get("defaultModel") or ""

    raw_rules = config.get("taskRules") or {}
    rule = parse_task_rule(raw_rules[task]) if task in raw_rules else None
    if rule is not None and not rule["any"] and not rule["per"]:
        rule = None

    tier_chain = list(global_tiers.get(tier) or []) or ([default_model] if default_model else [])

    # 1. Task rule models for this exact tier.
    if rule is not None and tier in rule["per"]:
        for model_id in rule["per"][tier]:
            push(model_id)

    escalate = tier == "high" and "high" in global_tiers
    if escalate:
        # 2a. high: global high chain first, then the task's any-tier models.
        for model_id in tier_chain:
            push(model_id)
        if rule is not None:
            for model_id in rule["any"]:
                push(model_id)
    else:
        # 2b. low/medium: task's any-tier models first, then the global chain.
        if rule is not None:
            for model_id in rule["any"]:
                push(model_id)
        for model_id in tier_chain:
            push(model_id)

    # 3. defaultModel as the last resort.
    push(default_model)

    head = out[0] if out else None
    matched = False
    if rule is not None and head is not None:
        matched = (rule["per"].get(tier) or [None])[0] == head or (rule["any"] or [None])[0] == head
    return out, matched


def select_model(config: dict, decision: Optional[dict]) -> dict:
    """Choose a model for a classified (or failed) request.

    ``decision`` is ``None`` when the classifier was unavailable/disabled;
    otherwise ``{taskLabel, complexityScore, complexityTier, ...}``.
    Returns ``{task, score, tier, model, candidates, fallback, taskRuleMatched}`` —
    ``model`` is the first candidate; an empty string means nothing is
    configured and the caller should keep the request's model.
    """
    if not decision:
        fallback_chain = [config.get("defaultModel")] if config.get("defaultModel") else []
        return {
            "task": "unclassified",
            "score": None,
            "tier": None,
            "model": fallback_chain[0] if fallback_chain else "",
            "candidates": fallback_chain,
            "fallback": True,
            "taskRuleMatched": False,
        }

    tier = resolve_tier(
        float(decision["complexityScore"]), str(decision["complexityTier"]),
        config.get("complexityBands"),
    )
    models, matched = candidates_for(config, str(decision["taskLabel"]), tier)
    model = models[0] if models else ""
    return {
        "task": decision["taskLabel"],
        "score": decision["complexityScore"],
        "tier": tier,
        "model": model,
        "candidates": models,
        "fallback": model == "",
        "taskRuleMatched": matched,
    }