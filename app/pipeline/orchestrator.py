"""Orchestrator: single entry point run_pipeline(correlation_id, message, thread_id).

Runs stages in order; handles clarification/refinement early exit; publishes response.
"""
import logging
import time
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


def run_pipeline(
    correlation_id: str,
    message: str,
    thread_id: str | None,
    t0_start: float | None = None,
) -> None:
    """Run the full pipeline: state_load -> classify -> plan -> clarify -> [resolve -> integrate] | early_exit.

    Publishes response (clarification, refinement, or completed) via queue.
    """
    t0 = t0_start if t0_start is not None else time.perf_counter()
    start_progress(correlation_id)

    ctx = PipelineContext(
        correlation_id=correlation_id,
        thread_id=(thread_id or "").strip() or None,
        message=(message or "").strip(),
    )

    def on_thinking(chunk: str) -> None:
        if chunk and str(chunk).strip():
            ctx.thinking_chunks.append(str(chunk).strip())
            send_to_user(correlation_id, {"type": "thinking", "content": str(chunk).strip()})
            logger.info("[thinking] %s", (chunk or "")[:80])

    try:
        trace_entered("pipeline.run_pipeline", correlation_id=correlation_id[:8], thread_id=thread_id or "")

        trace_entered(f"pipeline.stage.{STATE_LOAD}", correlation_id=correlation_id[:8])
        run_state_load(ctx)
        trace_entered(f"pipeline.stage.{CLASSIFY}", correlation_id=correlation_id[:8])
        run_classify(ctx, emitter=on_thinking)
        trace_entered(f"pipeline.stage.{PLAN}", correlation_id=correlation_id[:8])
        run_plan(ctx, emitter=on_thinking)

        store_plan(correlation_id, ctx.plan, thinking_log=ctx.thinking_chunks)

        trace_entered(f"pipeline.stage.{CLARIFY}", correlation_id=correlation_id[:8])
        resolvable = run_clarify(ctx, emitter=on_thinking)
        if not resolvable:
            _publish_clarification_or_refinement(ctx, t0)
            return

        trace_entered(f"pipeline.stage.{RESOLVE}", correlation_id=correlation_id[:8])
        run_resolve(ctx, emitter=on_thinking)
        trace_entered(f"pipeline.stage.{INTEGRATE}", correlation_id=correlation_id[:8])
        run_integrate(ctx, emitter=on_thinking)

        _publish_completed(ctx, t0)

    except Exception as e:
        logger.exception("Pipeline error: %s", e)
        _publish_failed(correlation_id, message, thread_id, ctx.thinking_chunks, e)


def _publish_clarification_or_refinement(ctx: PipelineContext, t0_start: float) -> None:
    """Build and publish clarification or refinement response."""
    duration_ms = int((time.perf_counter() - t0_start) * 1000)
    try:
        config_sha = get_config_sha() or None
    except Exception:
        config_sha = None

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
            "thinking_log": ctx.thinking_chunks,
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
            "thinking_log": ctx.thinking_chunks,
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
                thinking_log=ctx.thinking_chunks,
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
            )
        else:
            persistence.save_turn(
                correlation_id=ctx.correlation_id,
                question=ctx.refined_query or ctx.message,
                thinking_log=ctx.thinking_chunks,
                final_message=formatted,
                sources=[],
                duration_ms=duration_ms,
                model_used=None,
                llm_provider=None,
                thread_id=None,
                plan_snapshot=ctx.plan.model_dump() if ctx.plan else None,
                source_confidence_strip=None,
                config_sha=config_sha,
            )
        if ctx.thread_id:
            merged = {**(ctx.merged_state or {}), "refined_query": ctx.refined_query}
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
                thinking_log=ctx.thinking_chunks,
                final_message=ctx.final_message,
                sources=payload.get("sources", []),
                duration_ms=duration_ms,
                model_used=payload.get("model_used"),
                llm_provider=ctx.usages[0].get("provider") if ctx.usages else None,
                thread_id=ctx.thread_id,
                user_content=ctx.refined_query or ctx.message,
                assistant_content=ctx.final_message,
                plan_snapshot=ctx.plan.model_dump() if ctx.plan else None,
                source_confidence_strip=payload.get("source_confidence_strip"),
                config_sha=config_sha,
            )
        else:
            persistence.save_turn(
                correlation_id=ctx.correlation_id,
                question=ctx.refined_query or ctx.message,
                thinking_log=ctx.thinking_chunks,
                final_message=ctx.final_message,
                sources=payload.get("sources", []),
                duration_ms=duration_ms,
                model_used=payload.get("model_used"),
                llm_provider=ctx.usages[0].get("provider") if ctx.usages else None,
                thread_id=None,
                plan_snapshot=ctx.plan.model_dump() if ctx.plan else None,
                source_confidence_strip=payload.get("source_confidence_strip"),
                config_sha=config_sha,
            )
        if ctx.thread_id:
            merged = {**(ctx.merged_state or {}), "refined_query": ctx.refined_query}
            save_state_full(ctx.thread_id, merged)
    except Exception as e:
        logger.warning("Failed to persist turn: %s", e)

    clear_progress(ctx.correlation_id)
    store_response(ctx.correlation_id, payload)
    get_queue().publish_response(ctx.correlation_id, payload)
    logger.info("Response published for %s", ctx.correlation_id[:8])


def _publish_failed(
    correlation_id: str,
    message: str,
    thread_id: str | None,
    thinking_chunks: list[str],
    err: Exception,
) -> None:
    """Publish failed response."""
    from app.storage import store_response

    response_payload = {
        "status": "failed",
        "message": f"Something went wrong: {err}. Please try again.",
        "plan": None,
        "thinking_log": thinking_chunks,
        "response_source": "error",
        "model_used": None,
        "llm_error": str(err),
        "tokens_used": {"input_tokens": 0, "output_tokens": 0},
        "usage_breakdown": [],
        "cost_usd": 0.0,
        "sources": [],
        "source_confidence_strip": None,
        "cited_source_indices": [],
        "thread_id": thread_id,
    }
    clear_progress(correlation_id)
    store_response(correlation_id, response_payload)
    get_queue().publish_response(correlation_id, response_payload)
    logger.warning("Published failed response for %s: %s", correlation_id[:8], err)
