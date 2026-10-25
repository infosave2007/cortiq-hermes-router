"""Pure model/provider selection for the cortiq-*-router routing table."""

from __future__ import annotations

from typing import Any, Optional

from .providers import entry_key, is_provider_entry, normalize_entry

TIERS: tuple[str, ...] = ("low", "medium", "high")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def parse_task_rule(value: Any) -> dict[str, Any]:
    """Normalize a rule to ``{"any": [...], "per": {tier: [...]}}``.

    Entries stay in their original string/object form until endpoint
    normalization, so local model ids never get stringified as Python dicts.
    """
    if value is None:
        return {"any": [], "per": {}}
    if isinstance(value, (str, list)) or is_provider_entry(value):
        return {"any": _as_list(value), "per": {}}
    if isinstance(value, dict):
        any_models = _as_list(value.get("any", value.get("models")))
        per = {tier: _as_list(value[tier]) for tier in TIERS if tier in value}
        return {"any": any_models, "per": per}
    return {"any": [value], "per": {}}


def resolve_tier(score: float, router_tier: str, bands: Optional[dict] = None) -> str:
    """Bands are inclusive on their upper bound; otherwise use router tier."""
    if not bands:
        return router_tier
    if score <= bands["low"]:
        return "low"
    if score <= bands["medium"]:
        return "medium"
    return "high"


def candidate_entries_for(config: dict, task: str, tier: str) -> tuple[list[dict[str, Any]], bool]:
    """Ordered, deduplicated endpoint chain for ``(task, tier)``."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(raw_entry: Any) -> None:
        if raw_entry is None or raw_entry == "":
            return
        entry = normalize_entry(raw_entry)
        if not entry["model"]:
            return
        key = entry_key(entry)
        if key not in seen:
            seen.add(key)
            out.append(entry)

    global_tiers = config.get("globalTiers") or {}
    default_model = config.get("defaultModel") or ""
    raw_rules = config.get("taskRules") or {}
    rule = parse_task_rule(raw_rules[task]) if task in raw_rules else None
    if rule is not None and not rule["any"] and not rule["per"]:
        rule = None

    tier_chain = _as_list(global_tiers.get(tier)) or ([default_model] if default_model else [])
    if rule is not None and tier in rule["per"]:
        for entry in rule["per"][tier]:
            push(entry)

    escalate = tier == "high" and "high" in global_tiers
    if escalate:
        for entry in tier_chain:
            push(entry)
        if rule is not None:
            for entry in rule["any"]:
                push(entry)
    else:
        if rule is not None:
            for entry in rule["any"]:
                push(entry)
        for entry in tier_chain:
            push(entry)
    push(default_model)

    matched = False
    if rule is not None and out:
        head = entry_key(out[0])
        per_head = (rule["per"].get(tier) or [None])[0]
        any_head = (rule["any"] or [None])[0]
        matched = any(
            candidate is not None and candidate != "" and entry_key(normalize_entry(candidate)) == head
            for candidate in (per_head, any_head)
        )
    return out, matched


def candidates_for(config: dict, task: str, tier: str) -> tuple[list[str], bool]:
    """Backward-compatible model-id view of :func:`candidate_entries_for`."""
    entries, matched = candidate_entries_for(config, task, tier)
    return [entry["model"] for entry in entries], matched


def _selection(decision: Optional[dict], entries: list[dict[str, Any]], matched: bool,
               *, classifier_fallback: bool = False) -> dict[str, Any]:
    first = entries[0] if entries else None
    if decision is None:
        task, score, tier = "unclassified", None, None
    else:
        task, score = decision["taskLabel"], decision["complexityScore"]
        tier = None  # filled by select_model after band resolution
    return {
        "task": task,
        "score": score,
        "tier": tier,
        "model": first["model"] if first else "",
        "entry": first,
        "entries": entries,
        "candidates": [entry["model"] for entry in entries],
        "fallback": classifier_fallback or first is None,
        "taskRuleMatched": matched,
    }


def select_model(config: dict, decision: Optional[dict]) -> dict:
    """Choose the first endpoint while preserving legacy model-id fields."""
    if not decision:
        raw_default = config.get("defaultModel")
        entries = [normalize_entry(raw_default)] if raw_default else []
        return _selection(None, entries, False, classifier_fallback=True)

    tier = resolve_tier(
        float(decision["complexityScore"]), str(decision["complexityTier"]),
        config.get("complexityBands"),
    )
    entries, matched = candidate_entries_for(config, str(decision["taskLabel"]), tier)
    result = _selection(decision, entries, matched)
    result["tier"] = tier
    return result
