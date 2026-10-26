"""Thread-safe, durable aggregates for Hermes router decisions.

Only decision metadata is accepted into the recent buffer; request/response text
is never part of the schema. Daily buckets use UTC calendar days.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import logging
import math
import threading
import time
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

STATE_KEY = "statistics"
SCHEMA_VERSION = 2
RECENT_LIMIT = 200


#: Aggregate keys added in schema 1.1 (v0.3.0). Older state lacks them and is
#: upgraded in place by :func:`_upgrade_aggregate`; no field is renamed or removed.
_COUNTER_KEYS = ("by_provider", "by_route_kind", "fallback_reasons", "fallback_by_provider")


def empty_aggregate() -> dict[str, Any]:
    return {
        "calls": 0,
        "by_tier": {},
        "by_task": {},
        "by_model": {},
        "by_status": {},
        "by_provider": {},
        "by_route_kind": {},
        "fallback_reasons": {},
        "fallback_by_provider": {},
        "execution_fallback_count": 0,
        "step_continuation_count": 0,
        "fallback_count": 0,
        "avg_score": None,
        "score_count": 0,
        "avg_classifier_latency_ms": None,
        "classifier_latency_count": 0,
        "max_classifier_latency_ms": None,
    }


def _empty_state() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "timezone": "UTC",
        "totals": empty_aggregate(),
        "daily": {},
        "recent": [],
    }


def _upgrade_aggregate(aggregate: Any) -> dict[str, Any]:
    """Fill keys missing from an aggregate written by an older release."""
    if not isinstance(aggregate, dict):
        return empty_aggregate()
    for key, value in empty_aggregate().items():
        aggregate.setdefault(key, value)
    return aggregate


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def utc_timestamp(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


class StatsAggregator:
    """Serialize updates in-process and persist each immutable snapshot.

    State backend failures are deliberately non-fatal: the in-memory aggregate
    remains authoritative and a later successful write persists all decisions.
    """

    def __init__(self, state: Any = None) -> None:
        self._state = state
        self._lock = threading.RLock()
        self._data = _empty_state()
        stored = None
        if state is not None:
            try:
                stored = state.get(STATE_KEY, None)
                if isinstance(stored, dict):
                    self._data = stored
            except Exception as exc:
                logger.debug("cortiq-router: cannot load statistics: %s", exc)
            if not isinstance(self._data.get("totals"), dict):
                self._data = _empty_state()
            self._data["totals"] = _upgrade_aggregate(self._data["totals"])
            self._data["version"] = SCHEMA_VERSION
            if isinstance(self._data.get("daily"), dict):
                for day, aggregate in self._data["daily"].items():
                    self._data["daily"][day] = _upgrade_aggregate(aggregate)
            else:
                self._data["daily"] = {}
            if stored is None:
                self._migrate_last_decision()

    def _migrate_last_decision(self) -> None:
        """Seed statistics from the v0.1 single-decision state, preserving it."""
        try:
            old = self._state.get("last_decision", None)
        except Exception as exc:
            logger.debug("cortiq-router: cannot read last_decision for migration: %s", exc)
            return
        if not isinstance(old, dict):
            return
        try:
            at = float(old.get("at", time.time()))
        except (TypeError, ValueError):
            at = time.time()
        self.record({
            "task": old.get("task"),
            "score": old.get("score"),
            "tier": old.get("tier"),
            "model": old.get("model"),
            "originalModel": old.get("originalModel"),
            "fallback": old.get("fallback", False),
            "status": "classifier_error" if old.get("fallback") else "ok",
            "classifier_latency_ms": None,
        }, at=at)

    def _persist(self) -> None:
        if self._state is None:
            return
        try:
            self._state.set(STATE_KEY, copy.deepcopy(self._data))
        except Exception as exc:
            logger.debug("cortiq-router: cannot persist statistics: %s", exc)

    @staticmethod
    def _increment(bucket: dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    @classmethod
    def _update(cls, aggregate: dict[str, Any], entry: dict[str, Any]) -> None:
        aggregate["calls"] += 1
        cls._increment(aggregate["by_tier"], entry["tier"] or "(none)")
        cls._increment(aggregate["by_task"], entry["task"] or "(none)")
        cls._increment(aggregate["by_model"], entry["model"] or "(none)")
        cls._increment(aggregate["by_status"], entry["status"])
        if entry.get("step_continuation"):
            aggregate["step_continuation_count"] += 1
        if entry.get("provider"):
            cls._increment(aggregate["by_provider"], entry["provider"])
        cls._increment(aggregate["by_route_kind"], entry.get("route_kind") or "unchanged")
        if entry["fallback"]:
            aggregate["fallback_count"] += 1

        score = _number(entry["score"])
        if score is not None:
            count = aggregate["score_count"]
            previous = aggregate["avg_score"] or 0.0
            aggregate["avg_score"] = round((previous * count + score) / (count + 1), 6)
            aggregate["score_count"] = count + 1

        latency = _number(entry["classifier_latency_ms"])
        if latency is not None:
            count = aggregate["classifier_latency_count"]
            previous = aggregate["avg_classifier_latency_ms"] or 0.0
            aggregate["avg_classifier_latency_ms"] = round(
                (previous * count + latency) / (count + 1), 3
            )
            aggregate["classifier_latency_count"] = count + 1
            maximum = aggregate["max_classifier_latency_ms"]
            aggregate["max_classifier_latency_ms"] = (
                latency if maximum is None else max(maximum, latency)
            )

    def record(self, decision: Mapping[str, Any], *, at: Optional[float] = None) -> dict[str, Any]:
        """Record one decision and return its sanitized recent-buffer entry."""
        stamp = time.time() if at is None else float(at)
        entry = {
            "timestamp": utc_timestamp(stamp),
            "task": decision.get("task"),
            "score": _number(decision.get("score")),
            "tier": decision.get("tier"),
            "model": decision.get("model"),
            "originalModel": decision.get("originalModel"),
            "fallback": bool(decision.get("fallback", False)),
            "status": str(decision.get("status") or "ok"),
            "classifier_latency_ms": _number(decision.get("classifier_latency_ms")),
            "provider": decision.get("provider") or None,
            "route_kind": str(decision.get("route_kind") or "unchanged"),
            "step_continuation": bool(decision.get("step_continuation", False)),
        }
        day = entry["timestamp"][:10]
        with self._lock:
            self._update(self._data["totals"], entry)
            daily = self._data["daily"].setdefault(day, empty_aggregate())
            self._update(daily, entry)
            self._data["recent"].append(entry)
            if len(self._data["recent"]) > RECENT_LIMIT:
                del self._data["recent"][:-RECENT_LIMIT]
            self._persist()
            return copy.deepcopy(entry)

    def record_execution_fallback(self, provider: Optional[str], reason: str, *,
                                  at: Optional[float] = None) -> None:
        """Count one failed direct call that fell open to the Hermes model.

        ``reason`` is a short machine code (``timeout``, ``http_500``, ...); no
        request or response content is accepted here.
        """
        stamp = time.time() if at is None else float(at)
        code = str(reason or "unknown")[:64]
        name = str(provider or "unknown")[:64]
        day = utc_timestamp(stamp)[:10]
        with self._lock:
            for aggregate in (self._data["totals"],
                              self._data["daily"].setdefault(day, empty_aggregate())):
                aggregate["execution_fallback_count"] += 1
                self._increment(aggregate["fallback_reasons"], code)
                self._increment(aggregate["fallback_by_provider"], name)
            self._persist()

    @staticmethod
    def _combine(aggregates: list[dict[str, Any]]) -> dict[str, Any]:
        combined = empty_aggregate()
        score_sum = 0.0
        latency_sum = 0.0
        for aggregate in aggregates:
            aggregate = _upgrade_aggregate(aggregate)
            combined["calls"] += aggregate["calls"]
            combined["fallback_count"] += aggregate["fallback_count"]
            combined["execution_fallback_count"] += aggregate["execution_fallback_count"]
            combined["step_continuation_count"] += aggregate["step_continuation_count"]
            for name in ("by_tier", "by_task", "by_model", "by_status", *_COUNTER_KEYS):
                for key, count in aggregate[name].items():
                    combined[name][key] = combined[name].get(key, 0) + count
            score_count = aggregate["score_count"]
            if score_count:
                score_sum += aggregate["avg_score"] * score_count
                combined["score_count"] += score_count
            latency_count = aggregate["classifier_latency_count"]
            if latency_count:
                latency_sum += aggregate["avg_classifier_latency_ms"] * latency_count
                combined["classifier_latency_count"] += latency_count
            maximum = aggregate["max_classifier_latency_ms"]
            if maximum is not None:
                current = combined["max_classifier_latency_ms"]
                combined["max_classifier_latency_ms"] = (
                    maximum if current is None else max(current, maximum)
                )
        if combined["score_count"]:
            combined["avg_score"] = round(score_sum / combined["score_count"], 6)
        if combined["classifier_latency_count"]:
            combined["avg_classifier_latency_ms"] = round(
                latency_sum / combined["classifier_latency_count"], 3
            )
        return combined

    def aggregate(self, period: str = "all", *, now: Optional[float] = None) -> dict[str, Any]:
        """Return all-time, current UTC day, or rolling seven-UTC-day totals."""
        if period not in ("today", "week", "all"):
            raise ValueError("period must be today, week, or all")
        with self._lock:
            if period == "all":
                return copy.deepcopy(self._data["totals"])
            stamp = time.time() if now is None else float(now)
            date = datetime.fromtimestamp(stamp, timezone.utc).date()
            if period == "today":
                aggregate = self._data["daily"].get(date.isoformat())
                return copy.deepcopy(aggregate or empty_aggregate())
            dates = [(date - timedelta(days=offset)).isoformat() for offset in range(7)]
            return self._combine([
                self._data["daily"][day] for day in dates if day in self._data["daily"]
            ])

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= RECENT_LIMIT:
            raise ValueError(f"limit must be an integer from 1 to {RECENT_LIMIT}")
        with self._lock:
            return copy.deepcopy(self._data["recent"][-limit:][::-1])

    def reset(self) -> None:
        with self._lock:
            self._data = _empty_state()
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        """Return the full JSON-safe aggregate without exposing mutable state."""
        with self._lock:
            return copy.deepcopy(self._data)


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    text_rows = [["-" if value is None else str(value) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in text_rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def line(row: list[str]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()

    separator = "  ".join("-" * width for width in widths)
    return "\n".join([line(headers), separator, *(line(row) for row in text_rows)])


def _score(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.4f}"


def _latency(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.1f}"


def _summary_row(period: str, aggregate: dict[str, Any]) -> list[Any]:
    return [
        period,
        aggregate["calls"],
        aggregate["fallback_count"],
        _score(aggregate["avg_score"]),
        _latency(aggregate["avg_classifier_latency_ms"]),
        _latency(aggregate["max_classifier_latency_ms"]),
    ]


def _breakdown(title: str, values: dict[str, int]) -> str:
    rows = [[key, count] for key, count in sorted(
        values.items(), key=lambda item: (-item[1], item[0])
    )]
    return f"{title}\n{_table(['Name', 'Count'], rows)}"


def format_stats(
    stats: StatsAggregator,
    period: Optional[str] = None,
    *,
    now: Optional[float] = None,
) -> str:
    """Render period summaries and tier/task/model/status breakdowns."""
    selected = period or "all"
    aggregates = {
        name: stats.aggregate(name, now=now)
        for name in (("today", "week", "all") if period is None else (selected,))
    }
    labels = {"today": "Today", "week": "Week", "all": "All time"}
    lines = [
        "cortiq-hermes-router statistics (UTC; week = today + previous 6 days)",
        _table(
            ["Period", "Calls", "Fallback", "Avg score", "Avg cls ms", "Max cls ms"],
            [_summary_row(name, aggregate) for name, aggregate in aggregates.items()],
        ),
    ]
    aggregate = aggregates[selected]
    if not aggregate["calls"]:
        lines.append("(no decisions recorded for this period)")
        return "\n\n".join(lines)
    for key, title in (
        ("by_tier", "By tier"),
        ("by_task", "By task"),
        ("by_model", "By model"),
        ("by_status", "By status"),
        ("by_provider", "By provider"),
        ("by_route_kind", "By route kind"),
        ("fallback_reasons", "Execution fallbacks by reason"),
        ("fallback_by_provider", "Execution fallbacks by provider"),
    ):
        values = _upgrade_aggregate(aggregate).get(key) or {}
        if values:
            lines.append(_breakdown(f"{labels[selected]} · {title}", values))
    return "\n\n".join(lines)


def format_recent(entries: list[dict[str, Any]]) -> str:
    """Render newest-first decision metadata; request text is never present."""
    if not entries:
        return "cortiq-hermes-router recent decisions\n(no decisions recorded)"
    rows = [[
        entry["timestamp"],
        entry["status"],
        entry["task"],
        _score(entry["score"]),
        entry["tier"],
        entry["model"],
        "yes" if entry["fallback"] else "no",
        _latency(entry["classifier_latency_ms"]),
        entry["originalModel"],
        entry.get("provider") or "-",
        entry.get("route_kind") or "unchanged",
        "yes" if entry.get("step_continuation") else "no",
    ] for entry in entries]
    return "cortiq-hermes-router recent decisions (newest first, UTC)\n" + _table(
        ["Timestamp", "Status", "Task", "Score", "Tier", "Model", "Fallback", "Cls ms", "Original model", "Provider", "Route", "Tool step"],
        rows,
    )
