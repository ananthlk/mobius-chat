"""Orchestrator: single entry point run_pipeline(correlation_id, message, thread_id).

Runs stages in order; handles clarification/refinement early exit; publishes response.
"""
import logging
import os
import time
import traceback
from collections.abc import Callable

from app.chat_config import get_config_sha
from app.communication.agent import format_clarification, format_refinement_ask
from app.state.clarification_options import build_clarification_options
from app.communication.gate import send_to_user
from app.pipeline.context import PipelineContext
from app.persistence import get_persistence
from app.queue import get_queue
from app.storage import store_plan, store_response
from app.storage.progress import clear_progress, start_progress
from app.storage.threads import register_open_slots, save_state_full


from app.stages.state_load import run_state_load
from app.stages.classify import run_classify
from app.stages.plan import run_plan
from app.stages.clarify import run_clarify
from app.stages.resolve import run_resolve
from app.stages.integrate import run_integrate
from app.state.master_objective import MasterObjective, create_or_update_objective
from app.state.objective_eval import update_objective_from_answers, update_objective_from_integrator
from app.state.user_context_resolution import prefill_answer_set_from_master_objective, update_answer_set_from_user_context
from app.state.continuity_checks import extract_user_provided_context, user_wants_to_end_pursuit
from app.stages.continuity import should_ask_user_for_help, get_objective_end_state
from app.trace_log import trace_entered
from app.pipeline.stages import (
    STATE_LOAD,
    CLASSIFY,
    PLAN,
    CLARIFY,
    RESOLVE,
    INTEGRATE,
)

logger = logging.getLogger(__name__)


def _normalize_chat_mode(raw: str | None) -> str:
    m = (raw or "").strip().lower()
    if m == "agentic":
        return "agentic"
    if m == "task":
        return "task"
    return "copilot"


def _resolve_allowed_tools(
    *,
    mode: str,
    user_id: str | None,
    request_policy: list[str] | None = None,
) -> list[str] | None:
    """Compute the allowed-tool list for this turn.

    Resolution order (last wins):
      1. Mode default — task mode starts with [] (no tools); all other
         modes start with None (unrestricted).
      2. Per-user subscriptions from ``user_tool_subscriptions`` table —
         a user can enable extra tools or block defaults.
      3. Per-request override (``ChatRequest.tool_policy``, future field)
         — an API caller can further narrow the list for a single turn.

    Returns:
        ``None``  — no filter; pipeline uses all mode-appropriate tools.
        ``[]``    — all tools blocked.
        ``[...]`` — explicit non-empty allow-list.
    """
    # Mode baseline
    mode_defaults: list[str] | None = None
    if mode == "task":
        mode_defaults = []  # task mode: no tools by default

    # User subscriptions (best-effort — DB hiccup → degrade to mode default)
    try:
        from app.storage.tool_policy import get_allowed_tools_for_user
        allowed = get_allowed_tools_for_user(user_id, mode_defaults=mode_defaults)
    except Exception as exc:
        logger.debug("_resolve_allowed_tools: user-policy fetch failed (%s), using mode default", exc)
        allowed = mode_defaults

    # Per-request override (future hook — caller can pass e.g. ["search_corpus"])
    if request_policy is not None:
        # Intersect: only tools that are both in the user's list and the
        # request override.  If the user list was None (unrestricted),
        # the request policy becomes the new ceiling.
        if allowed is None:
            allowed = list(request_policy)
        else:
            req_set = frozenset(request_policy)
            allowed = [t for t in allowed if t in req_set]

    return allowed


# Human-readable labels for model emit (thinking panel)
_MODEL_LABELS = {
    "gemini-2.5-pro": "Gemini Pro",
    "gemini-2.5-flash": "Gemini Flash",
    "gemini-2.0-flash": "Gemini 2.0 Flash",
    "gemini-1.5-flash": "Gemini 1.5 Flash",
    "gemini-1.5-pro": "Gemini 1.5 Pro",
    "llama3.1:8b": "Llama 3.1 8B",
    "llama3.2:3b": "Llama 3.2 3B",
}


def _emit_model_summary(ctx: PipelineContext, react_duration_s: float, emitter: Callable[[str], None] | None) -> None:
    """Emit one line: model + latency (or 'Answered from report' when no usages)."""
    if not emitter:
        return
    usages = getattr(ctx, "usages", None) or []
    if not usages:
        if getattr(ctx, "active_skill_reference", False):
            emitter("Answered from report · {:.1f}s".format(react_duration_s or 0.1))
        return
    u = usages[-1]
    if u is None or not isinstance(u, dict):
        emitter("Unknown · {:.1f}s".format(react_duration_s or 0.1))
        return
    model = (u.get("model") or "").strip() or "unknown"
    model_label = _MODEL_LABELS.get(model, model.replace("gemini-", "Gemini ").title())
    latency_s = u.get("latency_s")
    if latency_s is None and u and "latency_ms" in u:
        latency_s = round((u["latency_ms"] or 0) / 1000.0, 2)
    latency_s = latency_s if latency_s is not None else round(react_duration_s, 2)
    if u.get("is_fallback"):
        emitter(f"{model_label} (fallback) · {latency_s}s")
    else:
        emitter(f"{model_label} · {latency_s}s")


DEBUG_PLAN = os.environ.get("MOBIUS_DEBUG_PLAN", "").lower() in ("1", "true", "yes")
# Default ReAct=1; treat missing or empty env as "1" so .env with MOBIUS_USE_REACT= doesn't disable ReAct
_use_react_val = (os.environ.get("MOBIUS_USE_REACT") or "1").strip().lower()
USE_REACT = _use_react_val in ("1", "true", "yes")

def _debug_plan_state(label: str, ctx: PipelineContext) -> None:
    """Print master plan, answers (with source), and parser plan when MOBIUS_DEBUG_PLAN=1 (for conversation_demo)."""
    if not DEBUG_PLAN:
        return
    lines = [f"\n  [DEBUG {label}]"]
    obj = ctx.master_objective
    if obj:
        status = obj.get("status", "?")
        summary = (obj.get("summary") or "")[:80]
        subs = obj.get("sub_objectives") or []
        lines.append(f"  master_objective: status={status} summary={summary!r}")
        for so in subs:
            ans = (so.get("answer") or "").strip()
            ans_part = f" | answer={ans[:50]}{'...' if len(ans) > 50 else ''}" if ans else ""
            lines.append(f"    - {so.get('id')}: {so.get('status')} | {(so.get('text') or '')[:50]}{ans_part}")
    else:
        lines.append("  master_objective: (none)")
    answer_set = getattr(ctx, "answer_set", None) or {}
    if answer_set:
        lines.append("  answer_set (source=planner|user_context|master_objective|rag|tool):")
        for sq_id, entry in sorted(answer_set.items()):
            src = entry.get("source", "?")
            ans = (entry.get("answer") or "")
            ans_display = ans[:60] + ("..." if len(ans) > 60 else "")
            lines.append(f"    - {sq_id}: source={src} | {ans_display}")
    plan = ctx.plan
    _subs = (getattr(plan, "subquestions", None) or []) if plan else []
    if _subs:
        lines.append("  plan.subquestions:")
        for sq in _subs:
            lines.append(f"    - {sq.id}: {(sq.text or '')[:60]}")
    else:
        lines.append("  plan: (none)")
    payload = getattr(ctx, "response_payload", None)
    if payload:
        closed = payload.get("closed_task_ids") or []
        open_ids = payload.get("open_task_ids") or []
        lines.append(f"  response_payload: closed={closed} open={open_ids}")
        res = payload.get("resolutions") or []
        if res:
            lines.append("  resolutions (integrator):")
            for r in res:
                sid = r.get("sq_id", "?")
                src = r.get("source", "?")
                res_text = (r.get("resolution") or "")
                res_display = res_text[:50] + ("..." if len(res_text) > 50 else "")
                lines.append(f"    - {sid}: source={src} | {res_display}")
    print("\n".join(lines))


def _invoke_cache_assist(ctx, *, chat_mode_hint: str | None, emitter) -> None:
    """Select cache-assist mode, invoke the cached_answer_lookup skill,
    record the result on ``ctx`` + emit signals. No return — all side
    effects land on the context.

    Active mode: the skill's result is appended to ``ctx.tool_results``
    so it appears in the reasoning context alongside search_corpus /
    google_search outputs naturally. Round 1 planner sees it as just
    another "prior tool result."

    Shadow mode: result stored on ``ctx.cache_candidates`` only, NOT
    appended to ``ctx.tool_results``. The LLM never sees it; the
    shadow-log writer in _publish_completed picks it up after the
    turn for agreement analytics.
    """
    from app.communication.emit_envelope import (
        make_cache_candidates_returned,
        make_cache_lookup_fired,
    )
    from app.services.cache_mode import select_cache_mode
    from app.skills.registry import SkillCall, dispatch, has as registry_has

    mode = select_cache_mode(
        correlation_id=ctx.correlation_id,
        chat_mode=chat_mode_hint or ctx.chat_mode,
        system_context=ctx.system_context,
        cache_assist_override=ctx.cache_assist_override,
        question=ctx.message or "",
    )
    ctx.cache_mode = mode

    if mode == "off":
        # Don't emit on off — uninteresting and would spam thinking_log.
        return

    if not registry_has("cached_answer_lookup"):
        logger.warning("cache-assist: skill not registered; skipping")
        ctx.cache_mode = "off"
        return

    # Compose caller-supplied filter profile. Chat's default is
    # "reasonable for a copilot/quick turn in the healthcare domain."
    # Specialized agents invoking this skill directly would pass their
    # own profile.
    import os
    try:
        default_max_age = int((os.environ.get("CACHE_ASSIST_DEFAULT_MAX_AGE_DAYS") or "14").strip())
    except (TypeError, ValueError):
        default_max_age = 14

    # Config_sha filter ties cache reads to the current prompts+LLM
    # config version. When we deploy a new config_sha, existing cache
    # entries quietly stop matching until re-seeded.
    try:
        from app.chat_config import get_config_sha
        config_sha = get_config_sha() or None
    except Exception:
        config_sha = None

    # Domain tags derived from the active payer/state so chat's cache
    # doesn't bleed across jurisdictions (a Florida Sunshine Health
    # turn shouldn't surface as a match for a Texas Medicaid question).
    active = (ctx.merged_state or {}).get("active") or {}
    dom_tags: list[str] = []
    if isinstance(active, dict):
        payer = (active.get("payer") or "").strip()
        state = (active.get("state") or "").strip()
        if payer:
            dom_tags.append(f"payer:{payer.lower().replace(' ', '_')}")
        if state:
            dom_tags.append(f"state:{state.lower()}")

    if emitter:
        emitter(make_cache_lookup_fired(
            correlation_id=ctx.correlation_id,
            mode=mode,
            thread_id=ctx.thread_id,
            user_id=ctx.user_id,
        ).to_dict())

    try:
        envelope = dispatch(SkillCall(
            name="cached_answer_lookup",
            inputs={
                "question": ctx.message or "",
                "max_age_days": default_max_age,
                "config_sha": config_sha,
                "domain_tags": dom_tags or None,
            },
            question=ctx.message or "",
            thread_id=ctx.thread_id,
            pipeline_ctx=ctx,
        ))
    except Exception as exc:
        logger.warning("cache-assist: skill dispatch failed: %s", exc)
        return

    extra = envelope.extra or {}
    candidates = extra.get("candidates") or []
    reasons = extra.get("reasons_filtered") or {}
    ctx.cache_candidates = candidates

    max_sim = max((c.get("similarity") or 0.0) for c in candidates) if candidates else None
    ages = [c.get("age_days") for c in candidates if c.get("age_days") is not None]
    if emitter:
        emitter(make_cache_candidates_returned(
            correlation_id=ctx.correlation_id,
            count=len(candidates),
            max_similarity=max_sim,
            oldest_age_days=max(ages) if ages else None,
            newest_age_days=min(ages) if ages else None,
            reasons_filtered=reasons,
            thread_id=ctx.thread_id,
            user_id=ctx.user_id,
        ).to_dict())

    if mode == "active" and envelope.text:
        # Surface to the reasoning LLM as a virtual tool result. The
        # existing build_reasoning_context loop iterates tool_results
        # and renders them; no react_loop change needed.
        tr_entry = {
            "tool": "cached_answer_lookup",
            "success": True,
            "result": envelope.text,
            "result_summary": f"{len(candidates)} cached candidate(s), max sim {max_sim:.2f}"
                              if max_sim is not None else "no cached candidates",
            "round_virtual": 0,
            "sources": [s.to_dict() for s in (envelope.sources or [])],
        }
        # tool_results lives on the react loop's local list; we stash
        # it on ctx so run_react can pick it up at start. A follow-up
        # commit can wire this directly into react_loop's tool_results
        # initialization. For now: use ctx.active_context as the
        # already-existing "pre-round-1 payload" surface (follow-up
        # context machinery is orthogonal to that path).
        #
        # Minimum-viable path: append to a new ctx field that
        # react_loop reads as a seed for tool_results.
        if not hasattr(ctx, "seed_tool_results") or ctx.seed_tool_results is None:
            ctx.seed_tool_results = []
        ctx.seed_tool_results.append(tr_entry)


def run_pipeline(
    correlation_id: str,
    message: str,
    thread_id: str | None,
    t0_start: float | None = None,
    use_react_override: bool | None = None,
    chat_mode: str | None = None,
    user_id: str | None = None,
    system_context: str | None = None,
    cache_assist: bool | None = None,
    user_profile: dict | None = None,
) -> None:
    """Run the full pipeline: state_load -> classify -> plan -> clarify -> [resolve -> integrate] | early_exit.

    Publishes response (clarification, refinement, or completed) via queue.

    ``user_id`` (Phase 2d completion, 2026-04-19): authenticated user_id
    from POST /chat's ``require_user`` dependency, forwarded through
    the queue payload. Stored on ``ctx.user_id`` and stamped onto the
    chat_turns row at ``persistence.save_turn(user_id=...)``. None in
    dev mode / when auth is disabled.
    """
    t0 = t0_start if t0_start is not None else time.perf_counter()
    start_progress(correlation_id)

    # Read at request time so we use current env (worker may have set MOBIUS_USE_REACT=1 after load_dotenv)
    env_use_react = (os.environ.get("MOBIUS_USE_REACT") or "1").strip().lower() in ("1", "true", "yes")
    if use_react_override is not None:
        use_react = use_react_override
    else:
        use_react = env_use_react

    # system_context (2026-04-22): normalize empty/whitespace to None so
    # downstream checks are simple `if ctx.system_context:` truthiness.
    _sys_ctx = (system_context or "").strip() or None

    ctx = PipelineContext(
        correlation_id=correlation_id,
        thread_id=(thread_id or "").strip() or None,
        message=(message or "").strip(),
        user_id=(user_id or "").strip() or None,
        system_context=_sys_ctx,
        cache_assist_override=cache_assist,
        user_profile=user_profile if isinstance(user_profile, dict) and user_profile else None,
    )

    def on_thinking(chunk) -> None:  # str | dict (EmitEnvelope.to_dict())
        """Accept legacy string emits OR structured envelope dicts.

        2026-04-19 (Sprint A.1 commit 1): added dict branch. The
        pipeline is migrating from bare strings to typed envelopes
        (see app/communication/emit_envelope.py). During rollout,
        both shapes coexist:

          - Legacy emit("◌ Searching…")   → string → appended as-is
          - New emit_env(envelope)        → dict   → appended as-is,
                                                     UI gets the
                                                     envelope's note
                                                     field for display
                                                     plus the full dict
                                                     under `envelope`
                                                     for structured
                                                     rendering.

        thinking_chunks therefore becomes a mixed array of strings
        and dicts during the rollout. The FE's is_envelope() helper
        distinguishes them. Once every emit site has migrated, only
        dicts appear.
        """
        from app.communication.emit_envelope import is_envelope

        if isinstance(chunk, dict) and is_envelope(chunk):
            # Structured envelope — store dict, extract note for UI.
            ctx.thinking_chunks.append(chunk)
            ui_text = (chunk.get("note") or f"[{chunk.get('signal', 'event')}]").strip()
            send_to_user(
                correlation_id,
                {"type": "thinking", "content": ui_text, "envelope": chunk},
            )
            # PHI hygiene (2026-04-20): only the signal + step reach the
            # app logger; the free-form ``note`` field can carry user
            # text (search queries, scraped URL titles) and therefore
            # stays out of logs. Operators who need the full thinking
            # trail read chat_turns.thinking_log from Postgres, which
            # is access-controlled separately.
            logger.info(
                "[thinking:%s] cid=%s step=%s",
                chunk.get("signal"),
                correlation_id[:8] if correlation_id else "",
                chunk.get("step_id", ""),
            )
            # Sprint A.2: conditionally POST promoted envelopes to
            # task-manager for chat-PM analytics. The writer itself
            # gates on MOBIUS_TASK_MANAGER_PROMOTION + the envelope's
            # report_to_task_manager flag, so this call is a no-op
            # for chat-side-only signals or when the feature is off.
            # Runs on a daemon thread — no latency added to the emit
            # path regardless of task-manager responsiveness.
            try:
                from app.services.task_manager_promotion import promote
                promote(chunk)
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("task-manager promotion hook raised: %s", e)
        elif chunk and str(chunk).strip():
            # Legacy path — bare string.
            s = str(chunk).strip()
            ctx.thinking_chunks.append(s)
            send_to_user(correlation_id, {"type": "thinking", "content": s})
            # PHI hygiene (2026-04-20): correlation_id only; the free-
            # form bare-string path historically carried search queries
            # and scraped page titles, so it must not reach server logs.
            logger.info(
                "[thinking:legacy] cid=%s",
                correlation_id[:8] if correlation_id else "",
            )

    # Distributed tracing span for the whole turn (Sprint 1 #11).
    # When CHAT_TRACE_ENABLED=0 this is a no-op context manager — zero
    # overhead on the disabled path. When on, it creates a span that
    # children (stages, LLM calls, tools) attach under via the OTel
    # current-context.
    from app.tracing_config import start_pipeline_span

    _pipeline_span_cm = start_pipeline_span(
        "pipeline.run_pipeline",
        correlation_id=correlation_id,
        thread_id=thread_id,
        user_id=user_id,
        extra={"chat_mode": chat_mode or "copilot"},
    )
    try:
        _pipeline_span_cm.__enter__()
    except Exception:
        # Defensive: a broken tracing init must never break the turn.
        _pipeline_span_cm = None

    try:
        trace_entered("pipeline.run_pipeline", correlation_id=correlation_id[:8], thread_id=thread_id or "")

        # Perceived-latency win (2026-04-22): emit an immediate "thinking"
        # line BEFORE state_load so the user sees motion within ~100ms of
        # POST /chat instead of waiting 1–3s for the first stage to
        # finish. Purely perceptual — no functional change — but the
        # difference between "blank panel for 2s" and "dot appears
        # instantly then updates" is the difference between feeling fast
        # and feeling stuck. Uses the same on_thinking path as every
        # other emit so it rides the SSE stream and lands in
        # thinking_log for replay.
        on_thinking("◌ Thinking…")

        # 2026-05-06: emit a personalization_applied envelope very
        # early so the user sees their mobius-user preferences are
        # being honored on this turn — and ops gets a per-cid
        # fingerprint of what was applied. Fires regardless of
        # whether the profile is populated; the data.applied=False
        # branch is observability for the negative case.
        try:
            from app.communication.emit_envelope import make_personalization_applied
            from app.pipeline.personalization import personalization_emit_payload
            on_thinking(make_personalization_applied(
                correlation_id=correlation_id,
                payload=personalization_emit_payload(ctx.user_profile),
                thread_id=ctx.thread_id,
                user_id=ctx.user_id,
            ).to_dict())
        except Exception as _pers_e:
            logger.debug("personalization emit skipped (%s); pipeline continues", _pers_e)

        # Preflight timing (2026-04-29) — surface the silent gap between
        # request receipt and first LLM call. Earlier follow-up traces
        # showed 11-15s of zero-log silence after USE_REACT=true and
        # before [vertex] generate_content; we couldn't tell what was
        # eating that time. These ``[preflight]`` markers let us pinpoint
        # the slow step (state_load DB queries, cache-assist HTTP, pronoun
        # resolution, active_skill build, etc.) without rebuilding logs.
        _t_pf = time.perf_counter()
        def _pf(label: str, t_prev: float) -> float:
            now = time.perf_counter()
            elapsed_ms = int((now - t_prev) * 1000)
            if elapsed_ms >= 50:  # only log nontrivial steps; spare the noise
                logger.info(
                    "[preflight] cid=%s step=%s elapsed_ms=%d",
                    correlation_id[:8], label, elapsed_ms,
                )
            return now

        trace_entered(f"pipeline.stage.{STATE_LOAD}", correlation_id=correlation_id[:8])
        run_state_load(ctx)
        _t_pf = _pf("state_load", _t_pf)

        # Cache-assist invocation (2026-04-23). Runs AFTER state_load so
        # chat_mode + merged_state are populated (the mode selector and
        # domain-tag builder both read from merged_state). Keeping this
        # in the orchestrator (not in react_loop) means the decision to
        # cache-assist is visible to every planning path, not just ReAct.
        #
        # Synchronous for MVP (~100–150ms per turn on active/shadow).
        # Parallelizing via asyncio.to_thread is tracked as a follow-up
        # — the win is small relative to round 1's 5–10s, and the
        # synchronous version keeps error handling obvious.
        try:
            _invoke_cache_assist(ctx, chat_mode_hint=chat_mode, emitter=on_thinking)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("cache-assist invocation failed (non-fatal): %s", exc)
        _t_pf = _pf("cache_assist", _t_pf)

        prev_mode = (ctx.merged_state or {}).get("last_chat_mode")
        if chat_mode is not None and str(chat_mode).strip():
            ctx.chat_mode = _normalize_chat_mode(str(chat_mode))
        else:
            ctx.chat_mode = _normalize_chat_mode(prev_mode if isinstance(prev_mode, str) else None)
        ctx.merged_state = {**(ctx.merged_state or {}), "last_chat_mode": ctx.chat_mode}

        # Resolve allowed tools: mode default ∩ user subscriptions ∩ request policy.
        # ctx.allowed_tools is None (no filter) or list[str] (explicit allow-list).
        # The ReAct loop and tool_manifest consume this to filter what the planner sees.
        ctx.allowed_tools = _resolve_allowed_tools(
            mode=ctx.chat_mode,
            user_id=getattr(ctx, "user_id", None),
        )

        obj_raw = (ctx.merged_state or {}).get("master_objective")
        has_active = bool(obj_raw and (obj_raw.get("status") or "active") == "active")
        if user_wants_to_end_pursuit(ctx.message or ""):
            if obj_raw:
                obj = MasterObjective.from_dict(obj_raw)
                if obj and obj.status == "active":
                    obj.status = "abandoned"
                    ctx.master_objective = obj.to_dict()
                    ctx.merged_state = {**(ctx.merged_state or {}), "master_objective": ctx.master_objective}
                    _publish_pursuit_ended(correlation_id, ctx, t0)
                    return
        else:
            ctx.user_provided_context = extract_user_provided_context(ctx.message or "", has_active)

        # Load master_objective into ctx so planner sees last_master_plan on follow-ups
        ctx.master_objective = (ctx.merged_state or {}).get("master_objective")

        # Conversational continuity: resolve pronoun/implicit references before planning
        from app.pipeline.message_resolver import (
            resolve_pronouns,
            detect_skill_reference,
            build_skill_context_summary,
        )

        last_failed = (ctx.merged_state or {}).get("last_failed_query") or {}
        prior_failed_question = last_failed.get("question") if isinstance(last_failed, dict) else None
        resolved_message, was_pronoun_enriched = resolve_pronouns(
            ctx.message,
            ctx.last_turns,
            prior_failed_question=prior_failed_question,
        )
        if was_pronoun_enriched:
            ctx.effective_message = resolved_message
            if on_thinking:
                on_thinking(f"↺ Understood: {(resolved_message or '')[:100]}")
        else:
            ctx.effective_message = ctx.message
        _t_pf = _pf("pronoun_resolve", _t_pf)

        # Active skill context: inject summary when message refers to it (no re-run)
        active_skill = (ctx.merged_state or {}).get("active_skill")
        is_skill_ref, skill_name = detect_skill_reference(ctx.effective_message, active_skill)
        if is_skill_ref and active_skill:
            skill_summary = build_skill_context_summary(active_skill)
            ctx.context_pack = (skill_summary + "\n\n" + (ctx.context_pack or "")).strip()
            if on_thinking and (active_skill.get("skill") or "").strip().lower() == "roster_report":
                on_thinking("Your report is stored. You can ask any question — answering from it.")
        ctx.active_skill_reference = bool(is_skill_ref)
        ctx.active_skill_name = skill_name
        _t_pf = _pf("active_skill_detect", _t_pf)

        if use_react:
            # ReAct path: Reason → Act → Observe; run_react sets ctx.plan, ctx.answers, ctx.answer_set, etc.
            logger.info("[pipeline] USE_REACT=true — taking ReAct path (no clarify/plan steps)")
            trace_entered("pipeline.stage.react", correlation_id=correlation_id[:8])
            t_react_start = time.perf_counter()
            try:
                from app.pipeline.react_loop import run_react
                run_react(ctx, emitter=on_thinking)
            except Exception as e:
                logger.exception("ReAct stage error: %s", e)
                _publish_failed(correlation_id, message, thread_id, ctx.thinking_chunks, e)
                return
            _emit_model_summary(ctx, time.perf_counter() - t_react_start, on_thinking)
            # Two-phase latency: emit ReAct answer immediately so the frontend renders
            # before the integrator's LLM call starts. The completed event replaces it.
            if getattr(ctx, "final_message", None):
                ctx.react_draft = ctx.final_message  # saved before integrator overwrites
                from app.storage.progress import append_draft_answer
                _mode_hint = "RECITAL" if getattr(ctx, "recital", None) else None
                append_draft_answer(ctx.correlation_id, ctx.final_message, mode_hint=_mode_hint)
            updates = {}
            if getattr(ctx, "failed_query", None):
                updates["last_failed_query"] = ctx.failed_query
            if getattr(ctx, "active_context", None):
                updates["active_context"] = ctx.active_context
            if updates:
                ctx.merged_state = {**(ctx.merged_state or {}), **updates}
            _debug_plan_state("PRE-INTEGRATOR", ctx)
        else:
            # Legacy path: classify → plan → clarify → resolve (only when MOBIUS_USE_REACT=0)
            logger.info("[pipeline] USE_REACT=false — taking legacy path (clarify → plan → resolve)")
            trace_entered(f"pipeline.stage.{CLASSIFY}", correlation_id=correlation_id[:8])
            run_classify(ctx, emitter=on_thinking)
            trace_entered(f"pipeline.stage.{PLAN}", correlation_id=correlation_id[:8])
            _debug_plan_state("PRE-PARSER", ctx)
            run_plan(ctx, emitter=on_thinking)

            store_plan(correlation_id, ctx.plan, thinking_log=(ctx.thinking_chunks if ctx.thinking_chunks is not None else []))

            if ctx.plan:
                is_new = ctx.classification == "new_question"
                obj = create_or_update_objective(ctx.plan, ctx.merged_state or {}, is_new_question=is_new)
                ctx.master_objective = obj.to_dict()
                ctx.merged_state = {**(ctx.merged_state or {}), "master_objective": ctx.master_objective}
            _debug_plan_state("POST-PARSER", ctx)

            trace_entered(f"pipeline.stage.{CLARIFY}", correlation_id=correlation_id[:8])
            try:
                resolvable = run_clarify(ctx, emitter=on_thinking)
            except Exception as e:
                logger.exception("Clarify stage error: %s", e)
                _publish_failed(correlation_id, message, thread_id, ctx.thinking_chunks, e)
                return
            if not resolvable:
                _publish_clarification_or_refinement(ctx, t0)
                return

            if ctx.classification in ("slot_fill", "jurisdiction_change"):
                ctx.answers = ["[No answer yet]"] * len(ctx.plan.subquestions or [])
                update_answer_set_from_user_context(ctx)
            prefill_answer_set_from_master_objective(ctx)

            trace_entered(f"pipeline.stage.{RESOLVE}", correlation_id=correlation_id[:8])
            try:
                run_resolve(ctx, emitter=on_thinking)
            except Exception as e:
                logger.exception("Resolve stage error: %s", e)
                _publish_failed(correlation_id, message, thread_id, ctx.thinking_chunks, e)
                return

            updates = {}
            if getattr(ctx, "failed_query", None):
                updates["last_failed_query"] = ctx.failed_query
            if getattr(ctx, "active_skill", None):
                updates["active_skill"] = ctx.active_skill
            if updates:
                ctx.merged_state = {**(ctx.merged_state or {}), **updates}

            obj_raw = ctx.master_objective
            obj = MasterObjective.from_dict(obj_raw) if obj_raw else None
            if obj and ctx.plan and ctx.answers:
                updated = update_objective_from_answers(
                    obj, ctx.plan, ctx.answers, ctx.retrieval_signals or []
                )
                if updated:
                    ctx.master_objective = updated.to_dict()
                    ctx.merged_state = {**(ctx.merged_state or {}), "master_objective": ctx.master_objective}

            if ctx.classification not in ("slot_fill", "jurisdiction_change"):
                update_answer_set_from_user_context(ctx)
            _debug_plan_state("PRE-INTEGRATOR", ctx)

        # Task mode: skip the integrator/composer entirely.
        # Return the ReAct loop's final answer as raw markdown.
        # The appeals agent (and any other programmatic caller) reads
        # raw_text directly from the completed SSE event and the poll
        # endpoint — it does not need an AnswerCard envelope.
        if ctx.chat_mode == "task":
            ctx.response_payload = {
                "raw_text": ctx.final_message or "",
                "status": "completed",
            }
            _publish_completed(ctx, t0)
            return

        # Status-only bypass: when the ReAct loop sets react_bypass_integrate=True
        # (e.g. still-indexing defer message), emit the final_message as plain
        # markdown without an LLM integrator round-trip or answer-card chrome
        # (confidence badge, sources block, etc.).
        if getattr(ctx, "react_bypass_integrate", False):
            ctx.response_payload = {
                "raw_text": ctx.final_message or "",
                "status": "completed",
            }
            _publish_completed(ctx, t0)
            return

        trace_entered(f"pipeline.stage.{INTEGRATE}", correlation_id=correlation_id[:8])
        try:
            on_thinking("Composing answer…")
            on_thinking("  (Integrator: turning reasoning + tool output into your answer card.)")
            run_integrate(ctx, emitter=on_thinking)
        except Exception as e:
            logger.exception("Integrate stage error: %s", e)
            _publish_failed(correlation_id, message, thread_id, ctx.thinking_chunks, e)
            return

        # Integrator may output resolved_subquestions when it used user_provided_context; update objective
        obj_raw = ctx.master_objective
        obj = MasterObjective.from_dict(obj_raw) if obj_raw else None
        integrator_data = ctx.response_payload if ctx.response_payload else ctx.final_message
        if obj and integrator_data:
            updated = update_objective_from_integrator(obj, integrator_data)
            if updated:
                ctx.master_objective = updated.to_dict()
                ctx.merged_state = {**(ctx.merged_state or {}), "master_objective": ctx.master_objective}
        _debug_plan_state("POST-INTEGRATOR", ctx)

        # User-as-leverage: when partial, add user_ask to payload (frontend can render below answer)
        ask_user, ask_msg = should_ask_user_for_help(ctx)
        if ask_user and ctx.response_payload:
            # Prefer integrator's next_questions_for_user when available (more specific)
            nq = ctx.response_payload.get("next_questions_for_user")
            if nq and isinstance(nq, list) and nq:
                first = nq[0]
                if isinstance(first, dict):
                    ctx.response_payload["user_ask"] = str(first.get("text") or "").strip() or ask_msg
                else:
                    ctx.response_payload["user_ask"] = str(first)
            elif ask_msg:
                ctx.response_payload["user_ask"] = ask_msg

        # Clear end state for UI (resolved | need_info | unable | user_ended | incomplete)
        obj_status, closure_msg = get_objective_end_state(ctx)
        if ctx.response_payload:
            ctx.response_payload["objective_status"] = obj_status
            if closure_msg:
                ctx.response_payload["closure_message"] = closure_msg

        # Product feedback (docs/feedback-agent-spec.md §6): surface the
        # planner's periodic nudge/survey decision, plus any capture_card a
        # product_feedback tool call returned. Both are additive — the frontend
        # ignores fields it doesn't render, so this is safe before the UI ships.
        if ctx.response_payload:
            if getattr(ctx, "offer_feedback", None):
                from app.pipeline.react.feedback_signal import enrich_offer_feedback
                ctx.response_payload["offer_feedback"] = enrich_offer_feedback(ctx.offer_feedback)
            if getattr(ctx, "capture_card", None):
                ctx.response_payload["capture_card"] = ctx.capture_card
            if getattr(ctx, "demo", None):
                ctx.response_payload["demo"] = ctx.demo
            elif not getattr(ctx, "demo", None):
                # Planner may answer directly from a skill's canned content
                # (response_source=plan) without invoking the skill as a tool,
                # so react_loop never runs and ctx.demo stays None. Cover that
                # path by keying off tool_fired at response assembly time.
                from app.communication.assistant_envelope import resolve_tool_fired
                _tf = resolve_tool_fired(ctx)
                if _tf == "document_upload_skill":
                    ctx.response_payload["demo"] = {
                        "script_id": "chat:upload-a-document",
                        "title": "Upload a document",
                    }

        # Rolling thread summary via a dedicated, focused LLM call. The
        # integrator's AnswerCard fields are unreliable on the production
        # model (Gemini emits a minimal card, drops thread_state, ignores
        # the label format); a narrow {short,long} task is reliable. Runs
        # here — AFTER the answer has already streamed — so it adds nothing
        # to the user-visible answer latency. Output overrides
        # ctx.thread_summary/thread_state (integrator value kept as
        # fallback when the call fails) and is persisted by
        # _publish_completed → upsert_thread_summary.
        if ctx.thread_id:
            try:
                import json as _json

                from app.responder.thread_summarizer import summarize_thread

                _ans = ""
                try:
                    _fm = _json.loads(ctx.final_message) if ctx.final_message else {}
                    _ans = (_fm.get("direct_answer") or "") if isinstance(_fm, dict) else ""
                except Exception:
                    _ans = ctx.final_message or ""
                _short, _long = summarize_thread(
                    previous_long=getattr(ctx, "previous_thread_summary", None),
                    user_message=ctx.refined_query or ctx.message,
                    answer_text=_ans,
                    jurisdiction_summary=getattr(ctx, "jurisdiction_summary", None),
                    correlation_id=ctx.correlation_id,
                    thread_id=ctx.thread_id,
                    mode=getattr(ctx, "chat_mode", None),
                )
                if _short:
                    ctx.thread_summary = _short
                if _long:
                    ctx.thread_state = _long
            except Exception as _e:  # noqa: BLE001 — never break a turn
                logger.warning("rolling summary generation failed (non-fatal): %s", _e)

        _publish_completed(ctx, t0)

    except Exception as e:
        if isinstance(e, TypeError):
            err_str = str(e).lower()
            if "not iterable" in err_str or "nonetype" in err_str:
                logger.error("NoneType/iterable TypeError in pipeline; full traceback:\n%s", traceback.format_exc())
        logger.exception("Pipeline error: %s", e)
        _publish_failed(correlation_id, message, thread_id, ctx.thinking_chunks, e)
        # Stamp the exception onto the tracing span for cross-reference
        # with Cloud Trace error views.
        if _pipeline_span_cm is not None:
            try:
                _pipeline_span_cm.__exit__(type(e), e, e.__traceback__)
                _pipeline_span_cm = None  # don't double-close in finally
            except Exception:
                pass
    finally:
        if _pipeline_span_cm is not None:
            try:
                _pipeline_span_cm.__exit__(None, None, None)
            except Exception:
                pass


def _publish_pursuit_ended(correlation_id: str, ctx: PipelineContext, t0_start: float) -> None:
    """Publish when user ends the relentless pursuit (never mind, that's enough, etc.)."""
    duration_ms = int((time.perf_counter() - t0_start) * 1000)
    msg = "Understood. Let me know if you'd like to ask something else."
    payload = {
        "status": "completed",
        "message": msg,
        "plan": ctx.plan.model_dump() if ctx.plan else None,
        "thinking_log": (ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
        "response_source": "pursuit_ended",
        "pursuit_ended": True,
        "objective_status": "user_ended",
        "model_used": None,
        "llm_error": None,
        "tokens_used": {"input_tokens": 0, "output_tokens": 0},
        "usage_breakdown": [],
        "cost_usd": 0.0,
        "sources": [],
        "source_confidence_strip": None,
        "cited_source_indices": [],
        "thread_id": ctx.thread_id,
    }
    try:
        config_sha = get_config_sha() or None
    except Exception:
        config_sha = None
    persistence = get_persistence()
    try:
        if ctx.thread_id:
            persistence.save_turn_with_messages(
                correlation_id=correlation_id,
                question=ctx.message,
                thinking_log=(ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
                final_message=msg,
                sources=[],
                duration_ms=duration_ms,
                model_used=None,
                llm_provider=None,
                thread_id=ctx.thread_id,
                user_content=ctx.message,
                assistant_content=msg,
                plan_snapshot=ctx.plan.model_dump() if ctx.plan else None,
                source_confidence_strip=None,
                config_sha=config_sha,
                user_id=ctx.user_id,
            )
            merged = {**(ctx.merged_state or {}), "refined_query": ctx.refined_query}
            if ctx.master_objective is not None:
                merged["master_objective"] = ctx.master_objective
            save_state_full(ctx.thread_id, merged)
    except Exception as e:
        logger.warning("Failed to persist pursuit-ended turn: %s", e)
    clear_progress(correlation_id)
    store_response(correlation_id, payload)
    get_queue().publish_response(correlation_id, payload)
    logger.info("Pursuit ended (user requested); response published for %s", correlation_id[:8])


def _publish_clarification_or_refinement(ctx: PipelineContext, t0_start: float) -> None:
    """Build and publish clarification or refinement response."""
    duration_ms = int((time.perf_counter() - t0_start) * 1000)
    try:
        config_sha = get_config_sha() or None
    except Exception:
        config_sha = None

    # Route clash: user message matched both web and RAG triggers
    if ctx.needs_route_clarification and ctx.route_clarification_choices:
        formatted = ctx.clarification_message or (
            "I can either search the web or search our policy materials. Which would you like?"
        )
        clarification_options = [
            {
                "slot": "route",
                "label": "How would you like to search?",
                "selection_mode": "single",
                "choices": ctx.route_clarification_choices,
                "allow_free_text": True,
                "free_text_hint": (
                    "Or describe what you want in your own words below (e.g. “policy manual only”), then press Send."
                ),
            }
        ]
        response_payload = {
            "status": "clarification",
            "message": formatted,
            "plan": ctx.plan.model_dump() if ctx.plan else None,
            "thinking_log": (ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
            "open_slots": ["route"],
            "clarification_options": clarification_options,
            "response_source": "clarification",
            "model_used": None,
            "llm_error": None,
            "tokens_used": {"input_tokens": 0, "output_tokens": 0},
            "usage_breakdown": [],
            "cost_usd": 0.0,
            "sources": [],
            "source_confidence_strip": None,
            "cited_source_indices": [],
            "thread_id": ctx.thread_id,
        }
        persistence = get_persistence()
        try:
            if ctx.thread_id:
                persistence.save_turn_with_messages(
                    correlation_id=ctx.correlation_id,
                    question=ctx.refined_query or ctx.message,
                    thinking_log=(ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
                    final_message=formatted,
                    sources=[],
                    duration_ms=duration_ms,
                    model_used=None,
                    llm_provider=None,
                    thread_id=ctx.thread_id,
                    user_content=ctx.refined_query or ctx.message,
                    assistant_content=formatted,
                    plan_snapshot=ctx.plan.model_dump() if ctx.plan else None,
                    source_confidence_strip=None,
                    config_sha=config_sha,
                    user_id=ctx.user_id,
                )
            else:
                persistence.save_turn(
                    correlation_id=ctx.correlation_id,
                    question=ctx.refined_query or ctx.message,
                    thinking_log=(ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
                    final_message=formatted,
                    sources=[],
                    duration_ms=duration_ms,
                    model_used=None,
                    llm_provider=None,
                    thread_id=None,
                    plan_snapshot=ctx.plan.model_dump() if ctx.plan else None,
                    source_confidence_strip=None,
                    config_sha=config_sha,
                    user_id=ctx.user_id,
                )
            if ctx.thread_id:
                merged = {**(ctx.merged_state or {}), "refined_query": ctx.refined_query}
                save_state_full(ctx.thread_id, merged)
        except Exception as e:
            logger.warning("Failed to persist route clarification turn: %s", e)
        clear_progress(ctx.correlation_id)
        store_response(ctx.correlation_id, response_payload)
        get_queue().publish_response(ctx.correlation_id, response_payload)
        logger.info("Route clarification published for %s", ctx.correlation_id[:8])
        return

    if ctx.needs_clarification and ctx.clarification_message:
        if ctx.thread_id and ctx.missing_slots:
            register_open_slots(ctx.thread_id, ctx.missing_slots)

        formatted = format_clarification(
            intent="jurisdiction",
            slots=ctx.missing_slots,
            raw_message=ctx.clarification_message,
        )
        clarification_options = build_clarification_options(ctx.missing_slots)
        response_payload = {
            "status": "clarification",
            "message": formatted,
            "plan": ctx.plan.model_dump() if ctx.plan else None,
            "thinking_log": (ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
            "open_slots": ctx.missing_slots,
            "clarification_options": clarification_options,
            "response_source": "clarification",
            "model_used": None,
            "llm_error": None,
            "tokens_used": {"input_tokens": 0, "output_tokens": 0},
            "usage_breakdown": [],
            "cost_usd": 0.0,
            "sources": [],
            "source_confidence_strip": None,
            "cited_source_indices": [],
            "thread_id": ctx.thread_id,
        }
    else:
        formatted = format_refinement_ask(
            original=ctx.message,
            suggestions=ctx.refinement_suggestions,
            raw_message="",
        )
        response_payload = {
            "status": "refinement_ask",
            "message": formatted,
            "plan": ctx.plan.model_dump() if ctx.plan else None,
            "thinking_log": (ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
            "suggestions": ctx.refinement_suggestions,
            "response_source": "refinement_ask",
            "model_used": None,
            "llm_error": None,
            "tokens_used": {"input_tokens": 0, "output_tokens": 0},
            "usage_breakdown": [],
            "cost_usd": 0.0,
            "sources": [],
            "source_confidence_strip": None,
            "cited_source_indices": [],
            "thread_id": ctx.thread_id,
        }

    persistence = get_persistence()
    try:
        if ctx.thread_id:
            persistence.save_turn_with_messages(
                correlation_id=ctx.correlation_id,
                question=ctx.refined_query or ctx.message,
                thinking_log=(ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
                final_message=formatted,
                sources=[],
                duration_ms=duration_ms,
                model_used=None,
                llm_provider=None,
                thread_id=ctx.thread_id,
                user_content=ctx.refined_query or ctx.message,
                assistant_content=formatted,
                plan_snapshot=ctx.plan.model_dump() if ctx.plan else None,
                source_confidence_strip=None,
                config_sha=config_sha,
                user_id=ctx.user_id,
            )
        else:
            persistence.save_turn(
                correlation_id=ctx.correlation_id,
                question=ctx.refined_query or ctx.message,
                thinking_log=(ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
                final_message=formatted,
                sources=[],
                duration_ms=duration_ms,
                model_used=None,
                llm_provider=None,
                thread_id=None,
                plan_snapshot=ctx.plan.model_dump() if ctx.plan else None,
                source_confidence_strip=None,
                config_sha=config_sha,
                user_id=ctx.user_id,
            )
        if ctx.thread_id:
            merged = {**(ctx.merged_state or {}), "refined_query": ctx.refined_query}
            if ctx.master_objective is not None:
                merged["master_objective"] = ctx.master_objective
            save_state_full(ctx.thread_id, merged)
    except Exception as e:
        logger.warning("Failed to persist clarification/refinement turn: %s", e)

    clear_progress(ctx.correlation_id)
    store_response(ctx.correlation_id, response_payload)
    get_queue().publish_response(ctx.correlation_id, response_payload)
    logger.info("Clarification/refinement published for %s", ctx.correlation_id[:8])


def _publish_completed(ctx: PipelineContext, t0_start: float) -> None:
    """Persist and publish completed response."""
    duration_ms = int((time.perf_counter() - t0_start) * 1000)
    payload = ctx.response_payload
    if not payload:
        return

    # Sprint A.1 commit 3: emit a structured turn_completed envelope
    # so task-manager promotion (A.2) can feed throughput, cost, and
    # rounds-distribution dashboards. Fires BEFORE the SSE + persist
    # steps so even if persistence fails downstream, the
    # turn-completed event still lands in thinking_log.
    try:
        from app.communication.emit_envelope import make_turn_completed

        tools_used = sorted({
            e.get("data", {}).get("tool")
            for e in (ctx.thinking_chunks or [])
            if isinstance(e, dict) and e.get("data", {}).get("tool")
        })
        # rounds_used: authoritative value tracked on ctx by run_react
        # at each iteration's top. More reliable than counting things
        # in thinking_chunks (which mixes strings + envelopes +
        # non-round-scoped entries) and handles all the loop-exit
        # paths uniformly (finalize, break, exception → integrator
        # fallback).
        rounds_used = int(getattr(ctx, "react_rounds_used", 0) or 0)
        total_tokens = None
        total_cost = None
        if ctx.usages:
            try:
                total_tokens = sum(
                    (u or {}).get("total_tokens", 0) or 0 for u in ctx.usages
                )
                total_cost = sum(
                    float((u or {}).get("cost_usd") or 0.0) for u in ctx.usages
                )
            except Exception:
                pass
        env = make_turn_completed(
            correlation_id=ctx.correlation_id,
            rounds_used=rounds_used,
            tools_used=list(tools_used),
            final_signal=",".join(ctx.retrieval_signals or []) or "unknown",
            duration_ms=duration_ms,
            total_llm_tokens=total_tokens,
            total_cost_usd=total_cost,
            thread_id=ctx.thread_id,
            user_id=ctx.user_id,
            integrator_mode=getattr(ctx, "integrator_mode", None),
        )
        # Record directly in thinking_chunks (no emitter here — this
        # fires at publish time, after run_react has returned).
        ctx.thinking_chunks.append(env.to_dict())
        # And promote (respects MOBIUS_TASK_MANAGER_PROMOTION flag +
        # the envelope's report_to_task_manager=True).
        from app.services.task_manager_promotion import promote
        promote(env.to_dict())
    except Exception as _e:  # pragma: no cover — defensive
        logger.warning("turn_completed envelope emit failed (non-fatal): %s", _e)

    # Large adjudication-only source blobs must not go to SSE/HTTP clients or in-memory response cache.
    client_payload = {k: v for k, v in payload.items() if k != "adjudication_sources"}
    # quick_mode: pass truncation flag so mini container can show "Full answer" link
    if getattr(ctx, "quick_truncated", False):
        client_payload["quick_truncated"] = True
    # system_context short-circuit (2026-04-22): expose a stable flag so
    # frontends can render a "answered from pre-loaded context" badge and
    # dashboards can bucket turns that bypassed the tool loop entirely.
    # Derived from ctx.retrieval_signals so a later refactor can't make
    # the flag and the signal drift apart.
    from app.services.doc_assembly import RETRIEVAL_SIGNAL_SYSTEM_CONTEXT
    if RETRIEVAL_SIGNAL_SYSTEM_CONTEXT in (ctx.retrieval_signals or []):
        client_payload["answered_from_system_context"] = True

    try:
        config_sha = get_config_sha() or None
    except Exception:
        config_sha = None

    persistence = get_persistence()
    try:
        if ctx.thread_id:
            persistence.save_turn_with_messages(
                correlation_id=ctx.correlation_id,
                question=ctx.refined_query or ctx.message,
                thinking_log=(ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
                final_message=ctx.final_message,
                sources=payload.get("sources", []),
                duration_ms=duration_ms,
                model_used=payload.get("model_used"),
                llm_provider=(ctx.usages[0] or {}).get("provider") if ctx.usages else None,
                thread_id=ctx.thread_id,
                user_content=ctx.refined_query or ctx.message,
                assistant_content=ctx.final_message,
                plan_snapshot=ctx.plan.model_dump() if ctx.plan else None,
                source_confidence_strip=payload.get("source_confidence_strip"),
                config_sha=config_sha,
                user_id=ctx.user_id,
                # Phase 13.7 — rolling thread summary from the integrator.
                # None for first-turn / parse-failure / non-success paths;
                # the persist path falls through to either the legacy
                # regex-based summary (insert_turn) or null.
                context_summary=getattr(ctx, "thread_summary", None),
            )
            # Canonical rolling summary: one row per thread, updated in
            # place each turn. summary_short drives the sidebar label;
            # summary_long is fed back as previous_thread_summary next turn.
            try:
                from app.storage.threads import upsert_thread_summary
                upsert_thread_summary(
                    ctx.thread_id,
                    getattr(ctx, "thread_summary", None),
                    getattr(ctx, "thread_state", None),
                )
            except Exception as _e:  # noqa: BLE001 — persistence is best-effort
                logger.warning("Failed to upsert rolling thread summary: %s", _e)
        else:
            persistence.save_turn(
                correlation_id=ctx.correlation_id,
                question=ctx.refined_query or ctx.message,
                thinking_log=(ctx.thinking_chunks if ctx.thinking_chunks is not None else []),
                final_message=ctx.final_message,
                sources=payload.get("sources", []),
                duration_ms=duration_ms,
                model_used=payload.get("model_used"),
                llm_provider=(ctx.usages[0] or {}).get("provider") if ctx.usages else None,
                thread_id=None,
                plan_snapshot=ctx.plan.model_dump() if ctx.plan else None,
                source_confidence_strip=payload.get("source_confidence_strip"),
                config_sha=config_sha,
                user_id=ctx.user_id,
            )
        if ctx.thread_id:
            merged = {**(ctx.merged_state or {}), "refined_query": ctx.refined_query}
            if ctx.master_objective is not None:
                merged["master_objective"] = ctx.master_objective
            save_state_full(ctx.thread_id, merged)
    except Exception as e:
        logger.warning("Failed to persist turn: %s", e)

    clear_progress(ctx.correlation_id)
    store_response(ctx.correlation_id, client_payload)
    get_queue().publish_response(ctx.correlation_id, client_payload)
    try:
        from app.services.post_run_adjudication import schedule_post_run_adjudication
        schedule_post_run_adjudication(ctx, payload)
    except Exception as e:
        logger.debug("schedule_post_run_adjudication: %s", e)
    # Cache-assist writer hook (2026-04-23). Fire-and-forget daemon
    # thread; gate check + write failures both log + swallow so cache
    # bookkeeping can never break a turn.
    try:
        from app.services.cache_writer import schedule_cache_write
        schedule_cache_write(ctx, payload)
    except Exception as e:
        logger.debug("schedule_cache_write: %s", e)
    # Stamp cache-assist bookkeeping onto the just-persisted chat_turns
    # row. Column-missing errors are swallowed inside the helper so
    # pre-migration DBs keep working.
    try:
        from app.storage.turns import update_turn_cache_mode
        _cands = getattr(ctx, "cache_candidates", []) or []
        _sims = [c.get("similarity") for c in _cands if c.get("similarity") is not None]
        update_turn_cache_mode(
            ctx.correlation_id,
            cache_mode=getattr(ctx, "cache_mode", "none"),
            cache_candidate_count=len(_cands),
            cache_top_similarity=max(_sims) if _sims else None,
            cache_influence=getattr(ctx, "cache_influence", "none") or "none",
        )
    except Exception as e:
        logger.debug("update_turn_cache_mode: %s", e)
    # Shadow-log writer — when cache_mode was 'shadow', persist the
    # candidates-that-would-have-been-shown alongside the fresh
    # answer so an offline agreement-scoring job can compare.
    try:
        if getattr(ctx, "cache_mode", "none") == "shadow":
            _write_cache_shadow_log(ctx, payload)
    except Exception as e:
        logger.debug("cache shadow log write failed: %s", e)
    # Fire post-synthesis grading callbacks for RAG OBSERVE rows.
    # corpus_search registers these when skip_synthesis=True (chat handles LLM);
    # we PATCH back with the final answer so synthesis_grade + ledger populate.
    _fire_rag_grade_callbacks(ctx)
    logger.info("Response published for %s", ctx.correlation_id[:8])


def _fire_rag_grade_callbacks(ctx: PipelineContext) -> None:
    """PATCH each pending RAG OBSERVE row with the final answer for grading.

    chat callers pass skip_synthesis=True to the RAG service, so synthesis_grade
    is NULL on prod rows. This fires after the chat LLM produces final_message,
    calling PATCH /observe/decisions/{rag_agent_id}/grade on each pending entry.
    Fire-and-forget threads; grading failures are logged but never block the turn.
    """
    pending = getattr(ctx, "pending_rag_grade_calls", None) or []
    if not pending:
        return
    final_answer = ctx.final_message or ""
    if not final_answer:
        return
    import json as _json
    import threading
    import urllib.request

    def _call(entry: dict) -> None:
        try:
            base_url = entry["base_url"].rstrip("/")
            rag_agent_id = entry["rag_agent_id"]
            url = f"{base_url}/api/observe/decisions/{rag_agent_id}/grade"
            body = _json.dumps({
                "answer": final_answer,
                "query": entry.get("query") or "",
                "chunks": entry.get("chunks") or [],
            }).encode()
            req = urllib.request.Request(url, data=body, method="PATCH",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
        except Exception as exc:
            logger.debug("rag grade callback failed for %s: %s", entry.get("rag_agent_id"), exc)

    for entry in pending:
        threading.Thread(target=_call, args=(entry,), daemon=True).start()


def _write_cache_shadow_log(ctx, payload: dict) -> None:
    """Persist one row to ``chat_cache_shadow_log`` for later A/B
    agreement analysis. Non-blocking (single DB insert). Never raises
    back to the orchestrator — shadow analytics must not affect live
    turns."""
    import json as _json

    from app.db_client import db_execute

    db_execute(
        """
        INSERT INTO chat_cache_shadow_log
            (correlation_id, question, config_sha, cached_candidates,
             fresh_final_message, fresh_sources_count, fresh_signals)
        VALUES (:cid, :q, :cfg, :cands::jsonb, :msg, :src_n, :sig)
        ON CONFLICT (correlation_id) DO NOTHING
        """,
        "chat",
        params={
            "cid": ctx.correlation_id,
            "q": (ctx.message or "").strip()[:2000],
            "cfg": (payload.get("config_sha") or "") or None,
            "cands": _json.dumps(getattr(ctx, "cache_candidates", []) or []),
            "msg": (payload.get("message") or payload.get("final_message") or "")[:4000],
            "src_n": int(len(payload.get("sources") or [])),
            "sig": ",".join(str(s) for s in (payload.get("retrieval_signals") or []) if s)[:500],
        },
    )


def _publish_failed(
    correlation_id: str,
    message: str,
    thread_id: str | None,
    thinking_chunks: list[str] | None,
    err: Exception,
) -> None:
    """Publish failed response. Always emits a structured payload; never raises."""
    from app.storage import store_response

    try:
        err_str = str(err) if err is not None else "Unknown error"
    except Exception:
        err_str = "Unknown error"
    # Classify the exception so the UI message is user-safe and the internal
    # detail (which may contain provider org IDs, tracebacks, etc.) stays out
    # of the outgoing payload.
    try:
        from app.communication.error_emit import classify_exception
        _env = classify_exception(err, tool="orchestrator") if err is not None else None
    except Exception:
        _env = None

    # Detect content-filter hits so the frontend can render a distinct amber
    # "content policy" state rather than a generic failure bubble.
    _CF_SIGNALS = (
        "output blocked by content filtering",
        "content filtering policy",
        "vertexblockederror",
    )
    _err_lower = err_str.lower() if err_str else ""
    _is_content_filtered = any(sig in _err_lower for sig in _CF_SIGNALS)

    chunks = list(thinking_chunks) if thinking_chunks is not None else []

    # Sprint A.1 commit 3: emit turn_failed envelope before building
    # the user-facing payload. Feeds top-level failure-rate dashboard
    # via task-manager promotion. Runs inside the try/except that
    # wraps the rest of _publish_failed so this cannot double-fail
    # the already-failing turn.
    try:
        from app.communication.emit_envelope import make_turn_failed
        from app.services.task_manager_promotion import promote

        error_class = type(err).__name__ if err is not None else "Unknown"
        stage = "orchestrator"
        if _env is not None:
            # classify_exception returns an ErrorEnvelope with an
            # error_code that helps distinguish failure types.
            try:
                stage = getattr(_env, "stage", None) or stage
            except Exception:
                pass
        env = make_turn_failed(
            correlation_id=correlation_id,
            error_class=error_class,
            stage=stage,
            error_message=err_str,
            last_tool=None,  # not easily recoverable at this layer
            thread_id=thread_id,
        )
        chunks.append(env.to_dict())
        promote(env.to_dict())
    except Exception:  # pragma: no cover — defensive
        # Must not double-fail: emission errors here are silent.
        pass
    # Phase 0.12: tighten the user message. The 0.6b version always suffixed
    # "Please try again." to whatever the classifier produced, which combined
    # poorly with classifier messages that already implied a retry
    # (e.g. "The model is temporarily busy — trying another option. Please
    # try again."). Per code-path:
    #   - recoverable errors (rate_limit, timeout, provider_error, scrape_failed)
    #     already include a retry hint in their message → pass through as-is
    #   - non-recoverable errors (auth_error, validation_error, internal_error)
    #     get a soft rephrase nudge.
    if _env is None:
        _user_message = (
            "I hit a problem finishing that answer. Please try rephrasing your question."
        )
    elif _env.is_recoverable:
        _user_message = _env.user_facing_message
    else:
        _user_message = (
            f"{_env.user_facing_message} Please try rephrasing your question."
        )
    if _is_content_filtered:
        _user_message = (
            "This response was blocked by a content safety rule. "
            "Try rephrasing or asking a more specific question."
        )
    response_payload = {
        "status": "failed",
        "message": _user_message,
        "error_envelope": _env.model_dump() if _env is not None else None,
        "plan": None,
        "thinking_log": chunks,
        "response_source": "content_filtered" if _is_content_filtered else "error",
        "model_used": None,
        "llm_error": err_str,
        "tokens_used": {"input_tokens": 0, "output_tokens": 0},
        "usage_breakdown": [],
        "cost_usd": 0.0,
        "sources": [],
        "source_confidence_strip": None,
        "cited_source_indices": [],
        "thread_id": thread_id,
    }
    try:
        clear_progress(correlation_id)
        store_response(correlation_id, response_payload)
        get_queue().publish_response(correlation_id, response_payload)
        logger.warning("Published failed response for %s: %s", correlation_id[:8], err_str)
    except Exception as e:
        logger.exception("Failed to publish error response for %s: %s", correlation_id[:8], e)
