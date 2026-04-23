"""Cache-assist mode selection — pure function, zero dependencies.

Called once per turn in the orchestrator. Returns one of:
  * ``"active"`` — cache candidates shown to the reasoning LLM
  * ``"shadow"`` — cache candidates logged but withheld (A/B bypass)
  * ``"off"``    — cache skipped entirely

Selection rules (first match wins):

    1. ``CACHE_ASSIST_ENABLED=0``        → off    (global kill switch)
    2. ``body.cache_assist == False``    → off    (per-turn opt-out)
    3. ``chat_mode == "agentic"``        → off    (always fresh by policy)
    4. ``ctx.system_context`` present    → off    (Round 0 takes precedence)
    5. question has freshness markers    → off    ("today"/"now"/"latest")
    6. bucket < CACHE_ASSIST_BYPASS_PCT  → shadow (A/B sample)
    7. otherwise                          → active

Bucketing is deterministic on ``correlation_id`` so the same request
always lands in the same bucket — replays / retries don't skew the
A/B distribution.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re

logger = logging.getLogger(__name__)


# Precompiled freshness markers. Whole-word matching so "todays" or
# "yesterday" don't false-trigger on "today"; "latest" won't match
# "latestimation" (made-up but makes the point).
_FRESHNESS_MARKER_RE = re.compile(
    r"\b(today|now|currently|latest|right now|as of (today|now)|this (week|month|year))\b",
    re.IGNORECASE,
)


def _env_enabled() -> bool:
    raw = (os.environ.get("CACHE_ASSIST_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _env_bypass_pct() -> int:
    try:
        n = int((os.environ.get("CACHE_ASSIST_BYPASS_PCT") or "10").strip())
        return max(0, min(100, n))
    except (TypeError, ValueError):
        return 10


def _bucket_for(correlation_id: str) -> int:
    """Deterministic 0–99 bucket from correlation_id.

    md5 is fine here — not cryptographic, just needs uniform hashing.
    First 8 hex chars give us 32 bits, mod 100 yields the bucket with
    acceptable bias."""
    if not correlation_id:
        return 0
    h = hashlib.md5(correlation_id.encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(h[:8], 16) % 100


def has_freshness_markers(question: str) -> bool:
    """Public for tests + for the orchestrator's debug logging."""
    if not question:
        return False
    return bool(_FRESHNESS_MARKER_RE.search(question))


def select_cache_mode(
    *,
    correlation_id: str,
    chat_mode: str | None,
    system_context: str | None,
    cache_assist_override: bool | None,
    question: str,
) -> str:
    """Decide cache mode for this turn. See module docstring for rules.

    Returns: one of ``"active"``, ``"shadow"``, ``"off"``.
    """
    if not _env_enabled():
        return "off"
    if cache_assist_override is False:
        return "off"
    mode = (chat_mode or "").strip().lower()
    if mode == "agentic":
        return "off"
    if system_context and system_context.strip():
        return "off"
    if has_freshness_markers(question or ""):
        return "off"

    bypass_pct = _env_bypass_pct()
    if bypass_pct >= 100:
        return "shadow"  # force-shadow for ops debugging
    if bypass_pct <= 0:
        return "active"  # A/B disabled
    if _bucket_for(correlation_id) < bypass_pct:
        return "shadow"
    return "active"
