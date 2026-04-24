"""Mode-aware composite weights for the Thompson-sampling bandit.

Sprint 2 #0.1 (2026-04-24). The bandit's composite score blends four
terms — quality, reliability, latency, cost — into a single scalar
that drives Beta-distribution updates. Until now every term got the
same 0.25 weight, which is why Flash (fast+cheap) and Haiku (quality+
fast) kept ending up tied: their strengths roughly cancel under equal
weights.

This module lets the weights bend with the user's intent:

  * ``fast``      — latency dominates. Used when the composer sends
                    a quick-mode request ("I need an answer, not a
                    dissertation").
  * ``normal``    — balanced (near the old equal weighting, with a
                    mild quality tilt). Copilot default.
  * ``thinking``  — quality dominates. Used when the composer flips
                    the "+ Thinking" toggle, or when agentic /
                    multi-hop ReAct fires (quality > latency when the
                    user already opted into depth).

Derivation from ``chat_mode`` when no explicit ``bandit_mode`` is
set by the caller:

    quick    → fast
    copilot  → normal
    agentic  → thinking

Stage overrides: some stages are intrinsically "thinking" regardless
of UX mode (``integrator``, ``integrator_roster``, ``critique``). A
``fast``-mode turn still runs the integrator at ``normal`` weights so
final-answer quality never collapses. See ``weights_for_stage()``.

The weights dict shape is:

    {"quality": 0.30, "reliability": 0.25, "latency": 0.25, "cost": 0.20}

Sum should be ~1.0 (normalized at lookup time, so small drift is fine).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ── Mode weight tables ────────────────────────────────────────────────

# Equal-weight baseline, kept as a sentinel for the tie-break path.
_EQUAL: dict[str, float] = {"quality": 0.25, "reliability": 0.25, "latency": 0.25, "cost": 0.25}

MODE_WEIGHTS: dict[str, dict[str, float]] = {
    "fast": {
        "quality":     0.15,
        "reliability": 0.20,
        "latency":     0.50,   # dominant
        "cost":        0.15,
    },
    "normal": {
        "quality":     0.30,
        "reliability": 0.25,
        "latency":     0.25,
        "cost":        0.20,
    },
    "thinking": {
        "quality":     0.55,   # dominant
        "reliability": 0.25,
        "latency":     0.10,
        "cost":        0.10,
    },
}

# Stages where we refuse to go below ``normal`` even when the request
# asks for ``fast``. Final-answer quality + groundedness gates must
# not collapse just because the UX mode said "quick".
_MIN_NORMAL_STAGES: frozenset[str] = frozenset({
    "integrator",
    "integrator_roster",
    "critique",
})


# ── chat_mode → bandit_mode mapping ──────────────────────────────────


def derive_bandit_mode(chat_mode: str | None) -> str:
    """Map the UX-level chat_mode to a bandit weighting mode.

    Unknown or missing values fall back to ``normal`` so callers that
    don't plumb chat_mode yet see today's balanced behavior.
    """
    raw = (chat_mode or "").strip().lower()
    if raw == "quick":
        return "fast"
    if raw == "agentic":
        return "thinking"
    # copilot, empty, anything else → normal
    return "normal"


# ── Lookup ───────────────────────────────────────────────────────────


def get_weights(mode: str | None) -> dict[str, float]:
    """Return a normalized weights dict for ``mode``. Missing keys or
    unknown mode → ``normal``. Always sums to 1.0."""
    key = (mode or "").strip().lower() or "normal"
    raw = MODE_WEIGHTS.get(key) or MODE_WEIGHTS["normal"]
    total = sum(raw.values()) or 1.0
    return {k: float(v) / float(total) for k, v in raw.items()}


def weights_for_stage(stage: str | None, mode: str | None) -> tuple[dict[str, float], str]:
    """Resolve the effective (mode, weights) pair for one stage.

    Enforces the minimum-normal floor for integrator/critique stages:
    if the caller asked for ``fast`` on one of those stages, we bump
    to ``normal`` and record the bump via the returned mode name so
    meta/logs can show the operator what happened.

    Returns ``(weights, effective_mode)``.
    """
    requested = (mode or "").strip().lower() or "normal"
    effective = requested
    st = (stage or "").strip().lower()
    if requested == "fast" and st in _MIN_NORMAL_STAGES:
        effective = "normal"
    return get_weights(effective), effective


__all__ = [
    "MODE_WEIGHTS",
    "derive_bandit_mode",
    "get_weights",
    "weights_for_stage",
]
