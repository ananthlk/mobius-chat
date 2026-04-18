"""Daily-token-quota tracker for the LLM bandit (Phase 2.5b).

Phase 2.5 made the bandit token-aware at the per-minute level (TPM):
candidates are dropped from the pool if a request's token count exceeds
the model's per-minute budget. But providers also enforce per-day budgets
(TPD) — especially Groq's free tier, where ``llama-3.3-70b-versatile``
has a 100_000 token/day cap. When that cap is near-exhausted, every
subsequent request fails with a 429 "Rate limit reached... tokens per
day (TPD)" and the user sees "model temporarily busy" on every turn
until the 24-hour window rolls. Observed live 2026-04-17:
99946 / 100_000 tokens used, three consecutive turns all blocked.

This module tracks usage per model over a rolling 24-hour window and
exposes the two primitives the router needs:

  - :func:`record_usage(model_id, tokens)` — log each completed LLM call
  - :func:`is_exhausted(model_id, spec_tpd, request_tokens)` — tell the
    router whether a candidate will hit the daily ceiling

It also supports short-circuit marking from a 429 retry hint:

  - :func:`mark_rate_limited_until(model_id, until_ts)` — when Groq
    returns "Please try again in 1h28m", skip this model for that long
    regardless of our usage math

Thread-safety: a single ``threading.Lock`` protects all state. Not
designed for multi-process — each worker tracks independently, which is
fine for the Groq free-tier case because the daily quota is per-org,
and any one worker filtering out the model protects the whole process
from further 429s on that turn. A future Redis-backed shared counter
would coordinate across workers; that's a separate phase.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# 24-hour rolling window. Providers report "per day (TPD)" but don't
# commit to a calendar-day reset — most use a sliding 24h window, so
# our tracker matches that.
_WINDOW_SECONDS = 24 * 60 * 60


# Safety margin around the declared TPD limit. Bandit treats a model as
# exhausted when (used_today + request_tokens) × safety > limit. 1.05
# leaves 5% headroom — enough slack to absorb the inaccuracy of our
# token estimate without wasting budget on borderline-safe calls.
_TPD_SAFETY = 1.05


@dataclass
class _ModelUsage:
    """Per-model state: rolling window of (timestamp, tokens) + an optional
    hard-skip deadline (set when a provider 429 says when to retry)."""

    # Append-only timestamps with token counts. Oldest entries drop off
    # lazily when we read; keeping it as a deque makes the drop O(k)
    # amortized (k = stale entries) rather than O(n).
    window: deque = field(default_factory=deque)
    # Absolute unix timestamp; while time.monotonic() < rate_limited_until
    # the model is skipped regardless of usage math. None = not flagged.
    rate_limited_until: float | None = None


_state: dict[str, _ModelUsage] = defaultdict(_ModelUsage)
_lock = threading.Lock()


# ── Public API ────────────────────────────────────────────────────────────


def record_usage(model_id: str, tokens: int) -> None:
    """Record that a call to ``model_id`` used ``tokens`` tokens.

    Callers pass prompt + completion (total tokens charged by the provider)
    when available. If the provider returned partial token info, pass the
    best estimate — undercounting here means the bandit will be slow to
    protect against exhaustion.
    """
    if not model_id or tokens <= 0:
        return
    now = time.monotonic()
    with _lock:
        state = _state[model_id]
        state.window.append((now, int(tokens)))
        # Opportunistic prune while we have the lock.
        _prune(state, now)


def get_used_today(model_id: str) -> int:
    """Sum of tokens recorded in the rolling 24-hour window. Thread-safe."""
    if not model_id:
        return 0
    now = time.monotonic()
    with _lock:
        state = _state.get(model_id)
        if state is None:
            return 0
        _prune(state, now)
        return sum(t for _ts, t in state.window)


def is_exhausted(
    model_id: str,
    spec_tpd: int | None,
    request_tokens: int,
) -> bool:
    """Return True if routing ``request_tokens`` to ``model_id`` would exceed
    the declared daily limit or the model is under a 429 retry-after hold.

    ``spec_tpd=None`` means unknown/unlimited → never exhausted by usage.
    Still honors a rate_limited_until hold regardless of TPD — a 429
    response means the provider has already decided we're over.
    """
    if not model_id:
        return False
    now = time.monotonic()
    with _lock:
        state = _state.get(model_id)
        if state is None:
            # No prior activity: not exhausted unless a hold was placed
            # without any recorded usage (unusual but possible).
            return False
        # Hard-skip hold from a 429 hint trumps the usage calculation.
        if state.rate_limited_until is not None:
            if now < state.rate_limited_until:
                return True
            # Hold expired — clear it so we don't keep reading a stale value.
            state.rate_limited_until = None

    if spec_tpd is None:
        return False
    used = get_used_today(model_id)  # takes lock internally
    projected = used + max(0, int(request_tokens))
    return int(projected * _TPD_SAFETY) > int(spec_tpd)


def mark_rate_limited_until(model_id: str, until_monotonic_ts: float) -> None:
    """Flag ``model_id`` as rate-limited until the given time.

    The caller computes the absolute ``time.monotonic()`` deadline from
    the provider's "try again in Xs" hint (see :func:`parse_retry_after_seconds`).
    During the hold, :func:`is_exhausted` returns True regardless of the
    usage window.
    """
    if not model_id:
        return
    with _lock:
        state = _state[model_id]
        state.rate_limited_until = until_monotonic_ts


def parse_retry_after_seconds(error_body: str) -> float | None:
    """Parse Groq/OpenAI/Anthropic 429 and 413 rate-limit hints.

    Handles the common forms observed in the wild:
      - "Please try again in 1h28m56.928s"       (Groq TPD exhaustion, 429)
      - "Please try again in 9m29.376s"          (Groq TPD, smaller window)
      - "Please try again in 45s"                (Groq TPM)
      - "Retry-After: 120"                       (generic HTTP)
      - "tokens per minute (TPM): Limit N..."    (Groq 413 — no explicit
                                                   retry time, so we return 60s
                                                   because TPM resets every minute)

    Returns float seconds, or None if no parseable hint is present. The
    caller converts to a monotonic deadline via
    ``time.monotonic() + parse_retry_after_seconds(err)``.
    """
    if not error_body:
        return None
    import re

    # Try "try again in Nh Nm Ns" / "try again in Nm Ns" / "try again in Ns".
    # Each section is optional; seconds can be fractional.
    m = re.search(
        r"try again in\s+"
        r"(?:(\d+)h)?\s*"
        r"(?:(\d+)m)?\s*"
        r"(?:(\d+(?:\.\d+)?)s)?",
        error_body,
        re.IGNORECASE,
    )
    if m and any(m.groups()):
        h = float(m.group(1) or 0)
        mi = float(m.group(2) or 0)
        s = float(m.group(3) or 0)
        total = h * 3600 + mi * 60 + s
        if total > 0:
            return total

    # Retry-After header form: either integer seconds or HTTP-date.
    m2 = re.search(r"Retry-After[:\s]+(\d+)", error_body, re.IGNORECASE)
    if m2:
        return float(m2.group(1))

    # 2026-04-17 Phase 2.5b follow-up: Groq's 413 TPM-overflow response
    # doesn't include an explicit retry-after — just "please reduce your
    # message size and try again". But TPM is a per-minute window, so a
    # 60-second hold is the correct default: by then the previous minute's
    # tokens have aged out and the bandit can try again (or pick another
    # model, which is what the hold is really protecting).
    #
    # Watch for "tokens per minute (TPM)" as the marker; don't want to
    # false-positive on TPD (per-day) which is handled above with the
    # actual retry-in hint.
    if re.search(
        r"tokens per minute\s*\(TPM\)",
        error_body,
        re.IGNORECASE,
    ):
        return 60.0

    return None


def reset(model_id: str | None = None) -> None:
    """Clear all state for one model (or the whole registry when None).

    Used by tests to ensure isolation between cases. Production callers
    should not need this — usage rolls off the 24h window naturally.
    """
    with _lock:
        if model_id is None:
            _state.clear()
        elif model_id in _state:
            del _state[model_id]


def snapshot() -> dict[str, dict]:
    """Read-only view of current state. Useful for /metrics or debug logs.

    Returns ``{model_id: {"used_today": int, "window_size": int,
    "rate_limited_until": float | None}}``.
    """
    now = time.monotonic()
    out: dict[str, dict] = {}
    with _lock:
        for mid, state in _state.items():
            _prune(state, now)
            out[mid] = {
                "used_today": sum(t for _ts, t in state.window),
                "window_size": len(state.window),
                "rate_limited_until": state.rate_limited_until,
            }
    return out


# ── Internal ──────────────────────────────────────────────────────────────


def _prune(state: _ModelUsage, now: float) -> None:
    """Drop entries older than the 24h window. Caller must hold _lock."""
    cutoff = now - _WINDOW_SECONDS
    while state.window and state.window[0][0] < cutoff:
        state.window.popleft()
