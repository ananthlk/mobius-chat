"""
Active context persistence.
Replaces active_skill — generic for any tool.
"""
from __future__ import annotations


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
    if merged_state and merged_state.get("active_context"):
        ctx = merged_state["active_context"]
        ttl = ctx.get("expires_after_turns", 0)
        if ttl > 0:
            ctx = {**ctx, "expires_after_turns": ttl - 1}
        return ctx
    for turn in (last_turns or [])[:1]:
        ctx = turn.get("active_context")
        if not ctx:
            continue
        ttl = ctx.get("expires_after_turns", 0)
        if ttl > 0:
            ctx = {**ctx, "expires_after_turns": ttl - 1}
        return ctx
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
