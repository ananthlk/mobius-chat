"""
Active context persistence.
Replaces active_skill — generic for any tool.
"""
from __future__ import annotations
import logging

from app.telemetry.spans import traced, record_decision

logger = logging.getLogger(__name__)


@traced("active_context")
def persist_active_context(
    ctx,
    turn_record: dict,
) -> dict:
    """Add active context to turn record before DB save."""
    active = getattr(ctx, "active_context", None)
    if active:
        turn_record["active_context"] = active
    failed = getattr(ctx, "failed_query", None)
    if failed:
        turn_record["failed_query"] = failed
    return turn_record


@traced("active_context")
def load_active_context(
    merged_state: dict | None,
    last_turns: list[dict] | None = None,
) -> dict | None:
    """
    Load most recent active context from merged_state (primary) or last_turns.
    merged_state is set from chat_state.state_json and already contains
    active_context when we persist it there. last_turns fallback for
    turn-level persistence if added later.
    """
    # NOTE — expires_after_turns is INERT IN ALL THREE DIRECTIONS.
    # Never written (the only assignments in app/ are the decrements below),
    # therefore never counted down (`.get(..., 0)` -> 0 -> `if ttl > 0` is
    # false), therefore never compared. Not "a TTL that does not expire" — a
    # TTL that does not exist, wearing the shape of one.
    #
    # My first version of this recording emitted `expired_but_served=(ttl<=0)`,
    # which is True on EVERY row for exactly that reason: a field that can hold
    # one value, passing "producer exists", "consumer exists" and "query returns
    # rows" while carrying nothing. That is the is_fallback finding I had named
    # hours earlier, reproduced in a field I added afterwards — which is the
    # argument for the rule being written down rather than remembered.
    #
    # So: record the RAW value and whether the KEY IS PRESENT AT ALL, because
    # "no writer" and "expired" must be distinguishable and were not.
    #
    # NOT fixed here on purpose: making the context actually expire changes what
    # the model sees on a later turn, which is a behaviour change needing a
    # ruling, not a telemetry pass. The recording makes it OBSERVABLE first —
    # `ttl_remaining=0` rows are the ones that should have expired.
    if merged_state and merged_state.get("active_context"):
        ctx = merged_state["active_context"]
        ttl = ctx.get("expires_after_turns", 0)
        if ttl > 0:
            ctx = {**ctx, "expires_after_turns": ttl - 1}
        record_decision("active_context", "carried_from_state", logger_=logger,
                        ttl_key_present=("expires_after_turns" in ctx),
                        ttl_raw=ctx.get("expires_after_turns"),
                        ttl_after=ctx.get("expires_after_turns", 0))
        return ctx
    for turn in (last_turns or [])[:1]:
        ctx = turn.get("active_context")
        if not ctx:
            continue
        ttl = ctx.get("expires_after_turns", 0)
        if ttl > 0:
            ctx = {**ctx, "expires_after_turns": ttl - 1}
        record_decision("active_context", "carried_from_last_turn", logger_=logger,
                        ttl_key_present=("expires_after_turns" in ctx),
                        ttl_raw=ctx.get("expires_after_turns"),
                        ttl_after=ctx.get("expires_after_turns", 0))
        return ctx
    # The negative branch is recorded too. A node that only emits when it finds
    # something is indistinguishable from one that is never called.
    record_decision("active_context", "none_available", logger_=logger,
                    had_state=bool(merged_state), had_last_turns=bool(last_turns))
    return None


def load_failed_query(
    merged_state: dict | None,
    last_turns: list[dict] | None = None,
) -> dict | None:
    """Load most recent failed query for pronoun resolution."""
    if merged_state and merged_state.get("last_failed_query"):
        return merged_state["last_failed_query"]
    for turn in (last_turns or [])[:3]:
        fq = turn.get("failed_query")
        if fq:
            return fq
    return None
