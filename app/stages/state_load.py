"""Stage: load state, apply delta from message, build context pack."""
import logging
from collections.abc import Callable

from app.pipeline.context import PipelineContext
from app.state.context_pack import build_context_pack
from app.state.context_router import route_context
from app.state.model import ThreadState
from app.state.state_extractor import extract_state_delta
from app.storage.results import clear_tool_results
from app.telemetry.spans import record_decision
from app.storage.threads import (
    StateUnavailable,
    get_last_turn_messages,
    get_state_with_version,
    get_thread_rolling_summary,
    save_state_tracked,
)
from app.storage.turns import get_last_turn_sources, get_prior_resolved_entities

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

    # THE RULE (P3 work order): state is only replaced from state that was
    # actually read. A failed read previously returned None, became `or {}`,
    # built a ThreadState from DEFAULT_STATE, and handed it to save_state_full
    # — "replace state entirely (no merge)". One transient error plus one
    # delta-bearing message destroyed the conversation, and state_version
    # incremented exactly as a healthy turn would, so nothing afterwards could
    # tell the difference.
    #
    # The turn still RUNS on defaults (a read failure should not fail the
    # user's question), but every write derived from this read is suppressed —
    # here, and at the three orchestrator sites and one react_loop site that
    # persist ctx.merged_state, which is this same read.
    _state_version: int | None = None
    try:
        raw_opt, _state_version = get_state_with_version(ctx.thread_id)
        raw = raw_opt or {}
    except StateUnavailable as exc:
        logger.warning(
            "[state_load] state unreadable for thread=%s (%s) — running this turn "
            "on defaults and SUPPRESSING all state writes; persisting now would "
            "replace the thread with defaults.",
            str(ctx.thread_id)[:8], exc,
        )
        raw = {}
        ctx.state_read_failed = True
    ctx.state_version = _state_version
    record_decision(
        "state_load", "state_unreadable" if ctx.state_read_failed
        else ("state_found" if raw else "state_absent"),
        logger_=logger, thread_id=str(ctx.thread_id)[:8],
        state_version=_state_version, keys=len(raw or {}),
    )
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
        save_state_tracked(ctx, to_save)

    merged = thread_state.to_dict()
    # Restore conversational continuity / ReAct fields not in ThreadState model (saved as full JSON)
    for key in ("active_skill", "last_failed_query", "active_context"):
        if key in raw and raw[key] is not None:
            merged[key] = raw[key]
    ctx.merged_state = merged
    # Carry report_run_id from previous turn so "ask about this report" can use it
    ctx.report_run_id = (merged.get("active") or {}).get("report_run_id")
    # ── the four independent reads, CONCURRENTLY ────────────────────────
    # Measured before changing: p50 1592ms, p95 5600ms over n=211 spans, and DB
    # reads account for ~1,796ms/turn — the reads ARE this node. Four of them
    # take nothing but thread_id and none consumes another's result, so they
    # were paying four round-trips for one round-trip's worth of dependency.
    #
    # `get_state_with_version` above is deliberately NOT in this group: it gates
    # every write in the turn (StateUnavailable must suppress them), so it stays
    # first and sequential where its failure semantics are obvious.
    #
    # Telemetry: the turn trace is a ContextVar that deliberately does not cross
    # a thread boundary — a shared span stack with no lock would mis-parent, and
    # concurrent writes to one span's counts dict can lose an update. So each
    # worker TIMES ITSELF and the parent records the db counts after the join,
    # the same pattern the integrator fan-out uses. Without this the per-table
    # attribution for this node would silently become orphaned counts.
    from concurrent.futures import ThreadPoolExecutor

    def _timed(fn, target: str, *a):
        import time as _t
        _t0 = _t.perf_counter()
        try:
            return fn(*a), target, (_t.perf_counter() - _t0) * 1000.0, None
        except Exception as exc:                      # never let a read kill the turn
            return None, target, (_t.perf_counter() - _t0) * 1000.0, exc

    _tid = ctx.thread_id
    _want_prior = bool(ctx.is_continuation)
    with ThreadPoolExecutor(max_workers=4) as _pool:
        _f_msgs = _pool.submit(_timed, get_last_turn_messages, "chat_turn_messages", _tid)
        _f_srcs = _pool.submit(_timed, get_last_turn_sources, "chat_turns", _tid)
        # prior_resolved_entities (2026-08-12, Task #90): gated on
        # is_continuation -- a fresh turn has nothing prior to resolve, and the
        # query would be pure overhead.
        _f_prior = _pool.submit(_timed, get_prior_resolved_entities, "chat_turns", _tid) \
            if _want_prior else None
        _f_summ = _pool.submit(_timed, get_thread_rolling_summary, "chat_threads", _tid)

    # RAW results kept alongside the defaulted ones. `_take` substitutes a
    # default for None so downstream code never handles it — but that
    # substitution is exactly the absent/empty collapse this node now reports
    # on, so the block report must read the raw value, not the defaulted one.
    # Caught by the test: last_turn_sources returning None rendered as "empty".
    _raw: dict[str, object] = {}

    def _take(fut, default, name: str = ""):
        if fut is None:
            _raw[name] = "__not_run__"
            return default
        val, target, ms, exc = fut.result()
        _raw[name] = val if exc is None else "__error__"
        try:
            from app.telemetry.spans import record_ambient, KIND_DB_READ
            record_ambient(KIND_DB_READ, target, ms=ms)
        except Exception:
            pass
        if exc is not None:
            logger.warning("[state_load] %s read failed: %s", target, exc)
            return default
        return val if val is not None else default

    ctx.last_turns = _take(_f_msgs, [], "last_turns")
    ctx.last_turn_sources = _take(_f_srcs, [], "last_turn_sources")
    ctx.prior_resolved_entities = _take(_f_prior, [], "prior_resolved")
    # Rolling rich context for the integrator. Prefer the canonical
    # per-thread brief (chat_threads.summary_long), updated in place each
    # turn; fall back to the latest non-null per-turn context_summary for
    # legacy threads predating migration 036.
    _prev_summary: str | None = _take(_f_summ, None, "rolling_summary")
    _summary_from_canonical = bool(_prev_summary)
    if not _prev_summary:
        for _turn in (ctx.last_turns or []):
            if not isinstance(_turn, dict):
                continue
            cs = (_turn.get("context_summary") or "").strip()
            if cs:
                _prev_summary = cs
                break
    ctx.previous_thread_summary = _prev_summary
    # WHICH BLOCKS WERE ASSEMBLED, AND WHICH WERE ABSENT — the decision this
    # node actually makes. Per cluster 1: the ABSENT ones are the signal. A
    # block that is missing and a block that is empty must be distinguishable,
    # so each records present/empty rather than a truthiness roll-up, and the
    # summary records WHERE it came from — the canonical per-thread brief or
    # the legacy per-turn fallback, which are different states of the corpus
    # that both render as "a summary exists".
    def _blk(name: str) -> str:
        v = _raw.get(name, "__missing__")
        if v == "__not_run__":
            return "not_run"
        if v == "__error__":
            return "read_failed"
        return "absent" if v is None else ("empty" if not v else "present")

    record_decision(
        "state_load", "blocks_assembled", logger_=logger,
        last_turns=_blk("last_turns"),
        last_turn_sources=_blk("last_turn_sources"),
        prior_resolved=("skipped_not_continuation" if not ctx.is_continuation
                        else _blk("prior_resolved")),
        rolling_summary=(_blk("rolling_summary") if _summary_from_canonical
                         else ("from_turn_fallback" if _prev_summary else "absent")),
        merged_keys=len(merged or {}),
    )
    route = route_context(ctx.message, merged, ctx.last_turns, reset_reason=reset_reason)
    record_decision(
        "state_load", f"route_{str(route).lower()}", logger_=logger,
        reset_reason=reset_reason, had_delta=bool(delta),
        open_slots=len(getattr(thread_state, "open_slots", None) or []),
        turns_available=len(ctx.last_turns or []),
    )

    # Improvements 3 & 5: on STANDALONE, evict slots and result cache so stale context doesn't bleed
    if route == "STANDALONE" and (thread_state.open_slots or thread_state.resolved_slots):
        thread_state.clear_slots()
        to_save = thread_state.to_dict()
        for key in ("active_skill", "last_failed_query", "active_context"):
            if key in merged and merged.get(key) is not None:
                to_save[key] = merged[key]
        save_state_tracked(ctx, to_save)
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
