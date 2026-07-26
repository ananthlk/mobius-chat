"""Control-plane vocabulary — the two real control variables, standardized.

Spec: docs/SPEC_LLMMANAGER_V2.md §4. Resolves the caller_mode / router-`mode`
vocabulary bug at the root by defining ONE canonical vocabulary for the two
control variables and a fail-closed normalization for every legacy alias.

  autonomy — "can we make decisions?"  → agentic | copilot
  speed    — "how fast?"               → real_time | background | batch

Key un-welding: the existing router `mode` param conflates BOTH (mode=copilot
secretly means assist-only + fast tiers). split_legacy_mode() separates them, so
agentic+fast and copilot+careful become expressible.

Fail-closed (Eval C3a): an unmapped value is REFUSED, never silently defaulted —
that silent default is exactly what produced three incompatible vocabularies.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

CANONICAL_AUTONOMY: frozenset = frozenset({"agentic", "copilot"})
CANONICAL_SPEED: frozenset = frozenset({"real_time", "background", "batch"})

# Legacy → canonical. Extend by adding a row (table-driven, §8 keeps it open).
_AUTONOMY_ALIASES: dict[str, str] = {
    "agentic": "agentic", "agent": "agentic", "act": "agentic", "autonomous": "agentic",
    "copilot": "copilot", "assist": "copilot", "suggest": "copilot", "advisory": "copilot",
}
_SPEED_ALIASES: dict[str, str] = {
    "real_time": "real_time", "realtime": "real_time", "interactive": "real_time", "fast": "real_time",
    "background": "background", "async": "background", "deferred": "background",
    "batch": "batch", "bulk": "batch", "offline": "batch",
    # Broadcaster-ratified legacy caller_mode → speed (spec §11, signed 2026-07-26).
    "chat.default": "real_time",   # default interactive
    "chat.copilot": "real_time",   # assisted fast tier
    "chat.thinking": "background", # deferred reasoning
    "auth_agent": "real_time",     # synchronous auth flow
    "research": "background",      # deferred research
    "copilot": "real_time",        # copilot = assist + fast
    "thinking": "background",      # thinking mode = deferred
}

# corpus_search.py STRATEGY-axis values that historically leaked into the
# caller_mode/speed field — the 3-vocabulary bug. These are NOT speed values.
# Surfacing a distinct, actionable error (rather than a generic "unmapped" or a
# silent default) is the whole point: coercing a strategy into an eligibility
# tier IS the bug. Route them to the strategy axis. (spec §11)
_STRATEGY_AXIS_VALUES: frozenset = frozenset({"score", "canonical_first", "balanced"})


class UnmappedControlValue(ValueError):
    """A control value has no explicit mapping rule. Fail-closed — never guess."""


class StrategyAxisValue(UnmappedControlValue):
    """A corpus_search STRATEGY value was passed on the speed axis. Route it to
    the strategy field — never coerce a strategy into a speed/eligibility tier
    (that mis-binding is the caller_mode bug). (spec §11)"""


class UnknownSpeedValue(UnmappedControlValue):
    """A truly-unknown speed value (not a legacy alias, not a strategy value) in
    the CODE path. Refuse — a real_time auto-default would mask a vocab leak, and
    real_time is least-cost but not least-harm (an unknown that meant `batch`
    would silently get fast/cheap models). (Eval C3a, §11)"""


def _normalize(raw: str | None, table: dict[str, str], axis: str) -> str:
    if raw is None:
        raise UnmappedControlValue(f"{axis}: value is None (must be explicit; no silent default)")
    key = str(raw).strip().lower().replace("-", "_")
    if key not in table:
        raise UnmappedControlValue(
            f"{axis}: {raw!r} has no mapping rule. Add one — do NOT default "
            f"(silent defaulting is the caller_mode bug)."
        )
    return table[key]


def normalize_autonomy(raw: str | None) -> str:
    return _normalize(raw, _AUTONOMY_ALIASES, "autonomy")


def normalize_speed(raw: str | None) -> str:
    """CODE-path normalization: callers pass control values they computed. An
    unrecognized speed here is a bug or a vocab leak — REFUSE, never default.
    For the legacy-DATA-ingestion boundary, use normalize_speed_lenient()."""
    # Reclassify (don't coerce) a strategy value that leaked onto the speed axis.
    if raw is not None and str(raw).strip().lower().replace("-", "_") in _STRATEGY_AXIS_VALUES:
        raise StrategyAxisValue(
            f"speed: {raw!r} is a corpus_search STRATEGY value, not a speed. "
            f"Route it to the strategy axis — do not bind it to an eligibility tier (§11)."
        )
    try:
        return _normalize(raw, _SPEED_ALIASES, "speed")
    except UnmappedControlValue as e:
        raise UnknownSpeedValue(str(e)) from None


def normalize_speed_lenient(
    raw: str | None, *, source: str = "legacy_store", stats: dict | None = None
) -> str | None:
    """LEGACY-INGESTION boundary ONLY — reading old stored caller_mode values
    during a batch migration. Do NOT use in the live path (there, normalize_speed
    must REFUSE so a vocab leak surfaces). Three tiers by severity, all fail-closed,
    none silent (Eval C3a, 2026-07-26):

      1. known legacy alias  → mapped speed.
      2. unknown string      → DEGRADE to 'real_time' + WARN. Crashing a live turn
         on one bad historical row is worse than a logged degraded default; this is
         SAFE (logged) degradation, not the silent default §4 forbids.
      3. strategy leak (score/canonical_first/balanced on the speed field) →
         QUARANTINE: return None (no value — caller SKIPS the row), log at ERROR
         (louder than the unknown WARN), and increment stats['strategy_leak_
         quarantined']. NOT re-raised (one corrupt row must not halt a batch of
         thousands of good rows — the exact failure this wrapper prevents) and NOT
         degraded to real_time (coercing cross-namespace-corrupt data into a valid
         tier would MASK it). The aggregate count is what surfaces SYSTEMIC
         corruption (a broken producer) so a human halts the migration off the
         METRIC, not off a single-row crash.

    `stats` is an optional caller-owned accumulator (the batch driver reads it to
    decide whether to halt); no module-level global state.
    """
    try:
        return normalize_speed(raw)
    except StrategyAxisValue:
        logger.error(
            "control_vocab: strategy value %r on the speed field from %s — "
            "QUARANTINED (skipping row; NOT coerced to a speed). A rising count "
            "here means a broken producer, not one bad row (§11/C3a).",
            raw, source,
        )
        if stats is not None:
            stats["strategy_leak_quarantined"] = stats.get("strategy_leak_quarantined", 0) + 1
        return None
    except UnknownSpeedValue:
        logger.warning(
            "control_vocab: unmapped legacy speed %r from %s → degrading to "
            "'real_time' (fail-closed WITH warning, not silent; §11/C3a)",
            raw, source,
        )
        return "real_time"


def split_legacy_mode(mode: str | None) -> tuple[str, str | None]:
    """Un-weld the legacy router `mode` into (autonomy, speed).

    The router welded them: mode=copilot historically meant assist-only AND
    fast-tier-only. So copilot → (copilot, real_time). agentic left speed
    unconstrained (the full model pool) → (agentic, None). Anything else is
    normalized as an autonomy value with speed unspecified.
    """
    if mode is None:
        raise UnmappedControlValue("legacy mode: None (must be explicit)")
    key = str(mode).strip().lower()
    if key == "copilot":
        return ("copilot", "real_time")   # copilot bundled fast tiers
    if key == "agentic":
        return ("agentic", None)          # full model pool
    return (normalize_autonomy(key), None)


def coverage_report(observed: set[str], axis: str) -> list[str]:
    """C3a coverage assertion: every observed value must have a rule. Returns the
    unmapped values (empty = fully covered) so seeding can fail-closed on them."""
    table = _AUTONOMY_ALIASES if axis == "autonomy" else _SPEED_ALIASES
    return sorted(v for v in observed if str(v).strip().lower().replace("-", "_") not in table)


# ── agent_role → temperature schedule (Q3 close, §5) ─────────────────────────
# RoundManager owns this signal, per round. Values are DEFAULTS to confirm with
# Eval ("pin the objective per module") — the ORDERING is the ratified part:
# explore (diverge) > synthesize (converge) > draft (commit).
_ROLE_TEMPERATURE: dict[str, float] = {
    "explore": 0.9,
    "synthesize": 0.35,
    "draft": 0.1,
}
_DEFAULT_ROLE_TEMPERATURE = 0.3  # unknown role → conservative; role should always be set


def temperature_for_role(agent_role: str | None) -> float:
    """Temperature for an execution phase. explore=high (diversity),
    synthesize/draft=low (consistency) — the Q3 objective split."""
    if agent_role is None:
        return _DEFAULT_ROLE_TEMPERATURE
    return _ROLE_TEMPERATURE.get(str(agent_role).strip().lower(), _DEFAULT_ROLE_TEMPERATURE)
