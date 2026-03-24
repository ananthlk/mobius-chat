"""Context router: decide STANDALONE | LIGHT | STATEFUL for context pack.

Fast path: regex rules (no LLM) — covers the common clear-cut cases.
Slow path: lightweight LLM classifier, invoked only when regex returns LIGHT (ambiguous).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Literal

import re

logger = logging.getLogger(__name__)

Route = Literal["STANDALONE", "LIGHT", "STATEFUL"]

# -------------------------------------------------------------------
# Expanded regex lists (spec §4.7)
# -------------------------------------------------------------------

PRONOUN_REF = re.compile(
    r"\b(that|this|those|these|it|them|their|its|the same)\b"
    # "prior" excluded — too ambiguous with "prior authorization" in healthcare context
    r"|\b(above|previous|earlier|the result)\b"
    r"|(what else|expand on|tell me more|more about|filter (those|them|that))"
    r"|(second|third|first) (one|result|match|item)"
    r"|(use the same|keep the same|same as before)",
    re.I,
)

NEW_TOPIC = re.compile(
    r"\b(new question|different topic|different question|new topic|switch to)\b",
    re.I,
)

# -------------------------------------------------------------------
# LLM classifier prompts (spec §4.4)
# -------------------------------------------------------------------

_ROUTE_CLASSIFIER_SYSTEM = """
You classify a user message as one of three conversation routes.
Output ONLY valid JSON. No other text.

Routes:
  STANDALONE  - New topic. No dependency on prior conversation.
  LIGHT       - Loosely related. Prior turn provides useful but optional context.
  STATEFUL    - Explicit reference to prior result, pronoun, or open question.
               Examples: 'What else did they find?', 'Expand on the second result',
               'Can you filter those?', 'Use the same state as before'

Output schema:
{
  "route": "STANDALONE" | "LIGHT" | "STATEFUL",
  "confidence": 0.0-1.0,
  "reason": "<10 words max>"
}
"""

_ROUTE_CLASSIFIER_USER = (
    "Last user message: {user_message}\n"
    "Prior turn summary: {prior_summary}\n"
    "Open slots: {open_slots}\n"
    "Output ONLY the JSON object:"
)

# Process-level cache: {cache_key: (result_dict, expires_at)} — spec §11 Q5
_ROUTE_CACHE: dict[str, tuple[dict, float]] = {}
_ROUTE_CACHE_TTL = 120  # seconds


def _cache_key(message: str, summary: str) -> str:
    return hashlib.sha256((message + summary).encode()).hexdigest()[:16]


def _cache_get(key: str) -> dict | None:
    entry = _ROUTE_CACHE.get(key)
    if not entry:
        return None
    result, expires_at = entry
    if time.time() > expires_at:
        _ROUTE_CACHE.pop(key, None)
        return None
    return result


def _cache_set(key: str, value: dict) -> None:
    _ROUTE_CACHE[key] = (value, time.time() + _ROUTE_CACHE_TTL)


def _llm_classify_route(message: str, summary: str, open_slots: list[str]) -> dict | None:
    """Call a cheap/fast LLM to classify ambiguous (LIGHT) routes. Returns None on any failure."""
    key = _cache_key(message, summary)
    cached = _cache_get(key)
    if cached:
        logger.debug("[router] LLM route from cache: %s", cached.get("route"))
        return cached
    try:
        import asyncio
        from app.services.llm_provider import get_llm_provider

        provider = get_llm_provider()
        slots_str = ", ".join(open_slots) if open_slots else "None"
        user_prompt = _ROUTE_CLASSIFIER_USER.format(
            user_message=message,
            prior_summary=summary or "None",
            open_slots=slots_str,
        )
        full_prompt = _ROUTE_CLASSIFIER_SYSTEM.strip() + "\n\n" + user_prompt
        raw, _ = asyncio.run(
            asyncio.wait_for(
                provider.generate_with_usage(full_prompt),
                timeout=1.0,
            )
        )
        if not raw or not raw.strip():
            return None
        # Strip markdown fences if present
        text = raw.strip()
        if "```" in text:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                text = text[start:end]
        result = json.loads(text)
        if result.get("route") not in ("STANDALONE", "LIGHT", "STATEFUL"):
            return None
        _cache_set(key, result)
        logger.info("[router] LLM route=%s confidence=%.2f reason=%s", result.get("route"), result.get("confidence", 0), result.get("reason", ""))
        return result
    except Exception as e:
        logger.debug("[router] LLM classifier failed (falling back to LIGHT): %s", e)
        return None


# -------------------------------------------------------------------
# Public interface
# -------------------------------------------------------------------

def route_context(
    user_text: str,
    existing_state: dict[str, Any],
    last_turns: list[dict[str, Any]],
    reset_reason: str | None = None,
) -> Route:
    """Return STANDALONE | LIGHT | STATEFUL for context pack building.

    Fast path (regex): covers payer changes, new-topic phrases, pronoun references,
    open slots, and existing payer/domain state. No LLM, no latency.

    Slow path (LLM): invoked only when regex returns LIGHT (ambiguous) and a prior
    turn summary is available. Adds ~200ms P50. Timeout 1s; falls back to LIGHT.
    """
    text = (user_text or "").strip().lower()
    active = (existing_state or {}).get("active") or {}
    payer = (active.get("payer") or "").strip()
    domain = (active.get("domain") or "").strip()
    open_slots = (existing_state or {}).get("open_slots") or []

    # --- Fast path: STANDALONE ---
    if reset_reason == "payer_change":
        return "STANDALONE"
    if NEW_TOPIC.search(text):
        return "STANDALONE"

    # --- Fast path: STATEFUL ---
    if PRONOUN_REF.search(text):
        return "STATEFUL"
    if open_slots:
        return "STATEFUL"
    if payer or domain:
        return "STATEFUL"

    # --- Slow path: LLM classifier for ambiguous LIGHT cases ---
    if last_turns:
        prior_summary = (
            (last_turns[0].get("context_summary") or "").strip()
            or (last_turns[0].get("assistant_content") or "")[:200]
        )
        if prior_summary:
            result = _llm_classify_route(user_text, prior_summary, list(open_slots))
            if result and result.get("confidence", 0) >= 0.70:
                route_val = result["route"]
                if route_val in ("STANDALONE", "LIGHT", "STATEFUL"):
                    return route_val  # type: ignore[return-value]

    return "LIGHT"
