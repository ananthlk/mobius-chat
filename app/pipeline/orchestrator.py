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
    return "agentic" if (raw or "").strip().lower() == "agentic" else "copilot"


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


def run_pipeline(
    correlation_id: str,
    message: str,
    thread_id: str | None,
    t0_start: float | None = None,
    use_react_override: bool | None = None,
    chat_mode: str | None = None,
    user_id: str | None = None,
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

    ctx = PipelineContext(
        correlation_id=correlation_id,
        thread_id=(thread_id or "").strip() or None,
        message=(message or "").strip(),
        user_id=(user_id or "").strip() or None,
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
            logger.info("[thinking:%s] %s", chunk.get("signal"), ui_text[:80])
        elif chunk and str(chunk).strip():
            # Legacy path — bare string.
            s = str(chunk).strip()
            ctx.thinking_chunks.append(s)
            send_to_user(correlation_id, {"type": "thinking", "content": s})
            logger.info("[thinking] %s", s[:80])

    try:
        trace_entered("pipeline.run_pipeline", correlation_id=correlation_id[:8], thread_id=thread_id or "")

        trace_entered(f"pipeline.stage.{STATE_LOAD}", correlation_id=correlation_id[:8])
        run_state_load(ctx)

        prev_mode = (ctx.merged_state or {}).get("last_chat_mode")
        if chat_mode is not None and str(chat_mode).strip():
            ctx.chat_mode = _normalize_chat_mode(str(chat_mode))
        else:
            ctx.chat_mode = _normalize_chat_mode(prev_mode if isinstance(prev_mode, str) else None)
        ctx.merged_state = {**(ctx.merged_state or {}), "last_chat_mode": ctx.chat_mode}

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

        _publish_completed(ctx, t0)

    except Exception as e:
        if isinstance(e, TypeError):
            err_str = str(e).lower()
            if "not iterable" in err_str or "nonetype" in err_str:
                logger.error("NoneType/iterable TypeError in pipeline; full traceback:\n%s", traceback.format_exc())
        logger.exception("Pipeline error: %s", e)
        _publish_failed(correlation_id, message, thread_id, ctx.thinking_chunks, e)


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

    # Large adjudication-only source blobs must not go to SSE/HTTP clients or in-memory response cache.
    client_payload = {k: v for k, v in payload.items() if k != "adjudication_sources"}
    # quick_mode: pass truncation flag so mini container can show "Full answer" link
    if getattr(ctx, "quick_truncated", False):
        client_payload["quick_truncated"] = True

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
            )
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
    logger.info("Response published for %s", ctx.correlation_id[:8])


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
    chunks = list(thinking_chunks) if thinking_chunks is not None else []
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
    response_payload = {
        "status": "failed",
        "message": _user_message,
        "error_envelope": _env.model_dump() if _env is not None else None,
        "plan": None,
        "thinking_log": chunks,
        "response_source": "error",
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
