"""AllaiGate classifier client — POST {routerUrl}/v1/route.

The request payload matches the cortiq-*-router family exactly: ``input`` is
a STRUCT (``{"text": ...}``), never a bare string. The response is validated
before use. Every failure mode (missing key, timeout, HTTP error, invalid
payload, unreachable host) degrades to ``None`` plus one log line — routing
never fails because of the classifier.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_VALID_TIERS = ("low", "medium", "high")


def build_route_body(text: str, profile: str = "balanced", taxonomy_id: Optional[str] = None,
                     max_chars: int = 12000) -> dict:
    """Build the ``/v1/route`` payload (input is a struct; truncated to ``max_chars``)."""
    body: dict[str, Any] = {
        "input": {"text": str(text or "")[:max_chars]},
        "options": {
            "policy_profile": profile or "balanced",
            "allow_oracle": True,
            "return_explanation": False,
            "top_k": 3,
        },
    }
    if taxonomy_id:
        body["taxonomy_id"] = taxonomy_id
    return body


def parse_route_decision(value: Any) -> Optional[dict]:
    """Validate the subset of the classifier response routing needs.

    Returns ``None`` (never coerces) for anything malformed.
    """
    if not isinstance(value, dict):
        return None
    decision = value.get("decision")
    if not isinstance(decision, dict):
        return None
    complexity = decision.get("complexity")
    if not isinstance(complexity, dict):
        return None

    def unit(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1

    task_label = decision.get("task_label")
    score = complexity.get("score")
    tier = complexity.get("tier")
    confidence = decision.get("confidence")
    request_id = value.get("request_id")
    is_novel = decision.get("is_novel")
    source = decision.get("source")
    if not (isinstance(task_label, str) and task_label.strip()
            and unit(score)
            and isinstance(tier, str) and tier in _VALID_TIERS
            and unit(confidence)
            and isinstance(request_id, str) and request_id
            and isinstance(is_novel, bool)
            and isinstance(source, str) and source):
        return None
    return {
        "taskLabel": task_label,
        "complexityScore": score,
        "complexityTier": tier,
        "confidence": confidence,
        "requestId": request_id,
        "isNovel": is_novel,
        "source": source,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are rejected, matching the family clients (redirect: 'error')."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def classify(text: str, config: dict, env: Optional[dict] = None,
             log: Callable[[str], None] = logger.warning,
             opener: Optional[urllib.request.OpenerDirector] = None) -> Optional[dict]:
    """Classify text; returns a validated decision or ``None`` — never raises."""
    env = os.environ if env is None else env
    key = env.get(config.get("apiKeyEnv", "CORTIQ_ROUTER_KEY"))
    if not key:
        log(f"cortiq-hermes-router: {config.get('apiKeyEnv', 'CORTIQ_ROUTER_KEY')} is not set — "
            "classification skipped, using fallback chain")
        return None

    base = str(config.get("routerUrl", "https://router.allaigate.com")).rstrip("/")
    payload = json.dumps(build_route_body(
        text,
        profile=config.get("routerProfile", "balanced"),
        taxonomy_id=config.get("taxonomyId"),
        max_chars=config.get("maxChars", 12000),
    )).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/v1/route",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    timeout = config.get("timeoutMs", 15000) / 1000.0
    active_opener = opener if opener is not None else _OPENER
    try:
        with active_opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                log(f"cortiq-hermes-router: classifier HTTP {status} — using fallback chain")
                return None
            raw = response.read()
    except urllib.error.HTTPError as exc:
        log(f"cortiq-hermes-router: classifier HTTP {exc.code} — using fallback chain")
        return None
    except Exception as exc:  # timeout / DNS / connection reset — degrade, never raise
        log(f"cortiq-hermes-router: classifier unreachable ({exc}) — using fallback chain")
        return None

    try:
        parsed = json.loads(raw)
    except ValueError:
        log("cortiq-hermes-router: invalid classifier response — using fallback chain")
        return None
    decision = parse_route_decision(parsed)
    if decision is None:
        log("cortiq-hermes-router: invalid classifier response — using fallback chain")
    return decision