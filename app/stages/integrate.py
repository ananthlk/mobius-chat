"""Stage: format response, build response payload."""
import json
from collections.abc import Callable
from typing import Any

from app.chat_config import get_config_sha
from app.communication.gate import send_to_user
from app.pipeline.context import PipelineContext
from app.responder import format_response
from app.services.cost_model import compute_cost
from app.state.jurisdiction import get_jurisdiction_from_active, jurisdiction_to_summary

# Badge keys for source_confidence_strip
BADGE_APPROVED_AUTHORITATIVE = "approved_authoritative"
BADGE_APPROVED_INFORMATIONAL = "approved_informational"
BADGE_PROCEED_WITH_CAUTION = "proceed_with_caution"
BADGE_AUGMENTED_WITH_GOOGLE = "augmented_with_google"
BADGE_INFORMATIONAL_ONLY = "informational_only"
BADGE_NO_SOURCES = "no_sources"

from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_CORPUS_ONLY,
    RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE,
    RETRIEVAL_SIGNAL_GOOGLE_ONLY,
    RETRIEVAL_SIGNAL_NO_SOURCES,
)


def _default_source_confidence(retrieval_signals: list[str], all_sources: list[dict]) -> str:
    """Compute default badge from retrieval signals."""
    if not retrieval_signals:
        return BADGE_NO_SOURCES
    if RETRIEVAL_SIGNAL_NO_SOURCES in retrieval_signals:
        return BADGE_NO_SOURCES
    if RETRIEVAL_SIGNAL_GOOGLE_ONLY in retrieval_signals:
        return BADGE_INFORMATIONAL_ONLY
    if RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE in retrieval_signals:
        return BADGE_AUGMENTED_WITH_GOOGLE
    labels = [s.get("confidence_label") for s in all_sources if s.get("confidence_label")]
    if any(l == "process_with_caution" for l in labels):
        return BADGE_PROCEED_WITH_CAUTION
    if all(l == "process_confident" for l in labels) and labels:
        return BADGE_APPROVED_AUTHORITATIVE
    if labels:
        return BADGE_APPROVED_INFORMATIONAL
    return BADGE_APPROVED_INFORMATIONAL


def run_integrate(
    ctx: PipelineContext,
    emitter: Callable[[str], None] | None = None,
) -> None:
    """Format response via integrator LLM, build response_payload."""
    plan = ctx.plan
    if not plan:
        return

    answers = ctx.answers
    all_sources = ctx.sources
    usages = ctx.usages
    retrieval_signals = ctx.retrieval_signals

    default_source_confidence = _default_source_confidence(retrieval_signals, all_sources)
    retrieval_metadata = {
        "default_source_confidence": default_source_confidence,
        "instruction": "We expect you to use the highest-rated document(s). If you override, set source_confidence_override and explain in confidence_note.",
    }
    sources_summary = [
        {"index": s.get("index", i + 1), "document_name": s.get("document_name") or "document", "confidence_label": s.get("confidence_label")}
        for i, s in enumerate(all_sources)
    ]

    def on_message_chunk(chunk: str) -> None:
        send_to_user(ctx.correlation_id, {"type": "final", "content": chunk})

    active = (ctx.merged_state or {}).get("active")
    jurisdiction_summary = None
    if active:
        j = get_jurisdiction_from_active(active)
        jurisdiction_summary = jurisdiction_to_summary(j) or None

    final_message, integrator_usage = format_response(
        plan,
        answers,
        user_message=ctx.message,
        emitter=emitter,
        message_chunk_callback=on_message_chunk,
        retrieval_metadata=retrieval_metadata,
        sources_summary=sources_summary,
        jurisdiction_summary=jurisdiction_summary,
    )
    ctx.final_message = final_message

    if integrator_usage:
        usages = list(usages) + [integrator_usage]
    else:
        usages = list(usages)

    total_input = sum(int(u.get("input_tokens") or 0) for u in usages)
    total_output = sum(int(u.get("output_tokens") or 0) for u in usages)
    total_cost = sum(compute_cost(u) for u in usages)
    model_used = (usages[0].get("model") or None) if usages else None

    response_sources = [
        {
            "index": s.get("index", i + 1),
            "document_id": s.get("document_id"),
            "document_name": s.get("document_name") or "document",
            "page_number": s.get("page_number"),
            "source_type": s.get("source_type"),
            "match_score": s.get("match_score"),
            "confidence": s.get("confidence"),
            "text": (s.get("text") or "")[:200],
        }
        for i, s in enumerate(all_sources)
    ]

    source_confidence_strip = default_source_confidence
    cited_source_indices: list[int] = []
    try:
        parsed = json.loads(final_message)
        if isinstance(parsed, dict):
            override = parsed.get("source_confidence_override")
            if override and str(override).strip() in (
                BADGE_APPROVED_AUTHORITATIVE,
                BADGE_APPROVED_INFORMATIONAL,
                BADGE_PROCEED_WITH_CAUTION,
                BADGE_AUGMENTED_WITH_GOOGLE,
                BADGE_INFORMATIONAL_ONLY,
                BADGE_NO_SOURCES,
            ):
                source_confidence_strip = str(override).strip()
            indices = parsed.get("cited_source_indices")
            if isinstance(indices, list):
                cited_source_indices = [
                    int(x) for x in indices
                    if isinstance(x, (int, float)) and 1 <= int(x) <= len(all_sources)
                ]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    usage_breakdown: list[dict[str, Any]] = []
    has_plan_usage = bool(getattr(plan, "llm_usage", None))
    for i, u in enumerate(usages):
        if i == 0 and has_plan_usage:
            stage = "plan"
        elif integrator_usage is not None and i == len(usages) - 1:
            stage = "integrator"
        else:
            stage = "rag"
        usage_breakdown.append({
            "stage": stage,
            "model": u.get("model") or "",
            "provider": u.get("provider") or "",
            "input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
            "cost_usd": round(compute_cost(u), 6),
        })

    try:
        config_sha = get_config_sha() or None
    except Exception:
        config_sha = None

    ctx.response_payload = {
        "status": "completed",
        "message": final_message,
        "plan": plan.model_dump(),
        "thinking_log": ctx.thinking_chunks,
        "response_source": "plan",
        "model_used": model_used,
        "llm_error": None,
        "tokens_used": {"input_tokens": total_input, "output_tokens": total_output},
        "usage_breakdown": usage_breakdown,
        "cost_usd": round(total_cost, 6),
        "sources": response_sources,
        "source_confidence_strip": source_confidence_strip,
        "cited_source_indices": cited_source_indices,
        "thread_id": ctx.thread_id,
    }
