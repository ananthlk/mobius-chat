"""Stage: load state, apply delta from message, build context pack."""
import logging
from collections.abc import Callable

from app.pipeline.context import PipelineContext
from app.state.context_pack import build_context_pack
from app.state.context_router import route_context
from app.state.model import ThreadState
from app.state.state_extractor import extract_state_delta
from app.storage.results import clear_tool_results
from app.storage.threads import get_last_turn_messages, get_state, save_state_full
from app.storage.turns import get_last_turn_sources

logger = logging.getLogger(__name__)


def run_state_load(
    ctx: PipelineContext,
    emitter: Callable[[str], None] | None = None,
    parse1_output=None,
    answer_card=None,
) -> None:
    """Load thread state, extract delta from message, apply_delta, save, build context pack."""
    if not ctx.thread_id or not (ctx.thread_id or "").strip():
        ctx.merged_state = {}
        ctx.last_turns = []
        ctx.context_pack = ""
        return

    raw = get_state(ctx.thread_id) or {}
    thread_state = ThreadState.from_dict(raw)

    # Capture prior payer before applying delta (for _prior_payer emit)
    prior_active = (thread_state.to_dict().get("active") or {})
    prior_payer = (prior_active.get("payer") or "").strip()

    delta, reset_reason = extract_state_delta(
        ctx.message, thread_state.to_dict(),
        parse1_output=parse1_output, answer_card=answer_card
    )
    if delta:
        thread_state.apply_delta(delta)
        to_save = thread_state.to_dict()
        for key in ("active_skill", "last_failed_query", "active_context"):
            if key in raw and raw[key] is not None:
                to_save[key] = raw[key]
        save_state_full(ctx.thread_id, to_save)

    merged = thread_state.to_dict()
    # Restore conversational continuity / ReAct fields not in ThreadState model (saved as full JSON)
    for key in ("active_skill", "last_failed_query", "active_context"):
        if key in raw and raw[key] is not None:
            merged[key] = raw[key]
    ctx.merged_state = merged
    # Carry report_run_id from previous turn so "ask about this report" can use it
    ctx.report_run_id = (merged.get("active") or {}).get("report_run_id")
    ctx.last_turns = get_last_turn_messages(ctx.thread_id)
    ctx.last_turn_sources = get_last_turn_sources(ctx.thread_id)
    # Phase 13.7 — pull the rolling thread summary from the most-recent
    # turn that has a non-null context_summary. Threaded into the
    # integrator so it can REFINE rather than rebuild. Newest-first
    # order in last_turns means we walk forward and stop on first
    # non-empty value.
    _prev_summary: str | None = None
    for _turn in (ctx.last_turns or []):
        if not isinstance(_turn, dict):
            continue
        cs = (_turn.get("context_summary") or "").strip()
        if cs:
            _prev_summary = cs
            break
    ctx.previous_thread_summary = _prev_summary
    route = route_context(ctx.message, merged, ctx.last_turns, reset_reason=reset_reason)

    # Improvements 3 & 5: on STANDALONE, evict slots and result cache so stale context doesn't bleed
    if route == "STANDALONE" and (thread_state.open_slots or thread_state.resolved_slots):
        thread_state.clear_slots()
        to_save = thread_state.to_dict()
        for key in ("active_skill", "last_failed_query", "active_context"):
            if key in merged and merged.get(key) is not None:
                to_save[key] = merged[key]
        save_state_full(ctx.thread_id, to_save)
        merged = thread_state.to_dict()
        for key in ("active_skill", "last_failed_query", "active_context"):
            if key in raw and raw[key] is not None:
                merged[key] = raw[key]
        ctx.merged_state = merged
    if route == "STANDALONE":
        clear_tool_results(ctx.thread_id)

    # Inject ephemeral jurisdiction metadata onto active for emit_jurisdiction_context().
    # These _private fields are read in run_resolve() and must NOT be persisted to the DB.
    merged_active = (merged.get("active") or {})
    new_payer = (merged_active.get("payer") or "").strip()
    if reset_reason:
        merged_active["_reset_reason"] = reset_reason
    if new_payer and not prior_payer:
        merged_active["_jurisdiction_new"] = True
    elif new_payer and prior_payer and new_payer.lower() != prior_payer.lower():
        merged_active["_prior_payer"] = prior_payer
        merged_active["_jurisdiction_new"] = False
    else:
        merged_active["_jurisdiction_new"] = False
    # Merge back (merged_active is a reference but re-assign to be safe)
    if merged.get("active") is not None:
        merged["active"] = merged_active
    ctx.merged_state = merged

    ctx.context_pack = build_context_pack(
        route, merged, ctx.last_turns, merged.get("open_slots") or [],
        last_turn_sources=ctx.last_turn_sources,
    )
