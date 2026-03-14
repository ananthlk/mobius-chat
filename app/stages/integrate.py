"""Stage: format response, build response payload."""
import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

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


def _default_source_confidence(
    retrieval_signals: list[str],
    all_sources: list[dict],
    answer_set: dict | None = None,
) -> str:
    """Compute default badge from retrieval signals. Layer-aware when answer_set provides layer_used."""

    # Layer-based override — takes priority over signal when layer_used is present
    if answer_set:
        layers = [v.get("layer_used") for v in answer_set.values() if isinstance(v, dict)]
        layers = [l for l in layers if l is not None]
        if layers:
            max_layer = max(layers)
            if max_layer == 5:
                return BADGE_NO_SOURCES
            if max_layer == 4:
                return BADGE_INFORMATIONAL_ONLY
            if max_layer == 3:
                has_url_source = any(
                    s.get("url") or s.get("source_type") == "web" for s in all_sources
                )
                return BADGE_APPROVED_INFORMATIONAL if has_url_source else BADGE_INFORMATIONAL_ONLY
            # max_layer <= 2: fall through to existing signal-based logic

    # Existing signal-based logic (unchanged)
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

    default_source_confidence = _default_source_confidence(
        retrieval_signals, all_sources, answer_set=ctx.answer_set
    )
    retrieval_metadata = {
        "default_source_confidence": default_source_confidence,
        "instruction": "We expect you to use the highest-rated document(s). If you override, set source_confidence_override and explain in confidence_note.",
    }

    # Mode cap: if any subquestion was answered by Layer 4 (reasoning), CANONICAL is not permitted
    layer4_used = any(
        (v.get("layer_used") or 0) >= 4
        for v in (ctx.answer_set or {}).values()
        if isinstance(v, dict)
    )
    if layer4_used:
        retrieval_metadata["layer4_used"] = True
        retrieval_metadata["instruction"] = (
            retrieval_metadata["instruction"]
            + " NOTE: One or more answers came from general reasoning (Layer 4)."
            " Set mode to FACTUAL or BLENDED — never CANONICAL for Layer 4 content."
        )
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
        user_provided_context=getattr(ctx, "user_provided_context", None),
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
    resolutions: list[dict[str, Any]] = []
    closed_task_ids: list[str] = []
    open_task_ids: list[str] = []
    next_steps: list[str] = []
    next_questions_for_user: list[str] = []
    # When we cannot parse the response (LLM error, plain text), show a friendly try-again card
    FALLBACK_TRY_AGAIN = "Something went wrong. Please try again, or start a new chat."
    display_message: str = final_message or ""
    try:
        raw = (final_message or "").strip()
        # Strip "json " prefix (LLM sometimes returns "json {...}")
        if raw.lower().startswith("json "):
            raw = raw[5:].lstrip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines).strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            # Extract display_message for frontend AnswerCard (avoids raw JSON in card)
            da = parsed.get("direct_answer")
            secs = parsed.get("sections")
            if isinstance(da, str) and isinstance(secs, list):
                # direct_answer sometimes contains raw JSON (LLM nested resolutions inside it)
                da_stripped = da.strip()
                if da_stripped.startswith("```json") or (da_stripped.startswith("{") and ("resolutions" in da_stripped[:200] or "direct_answer" in da_stripped[:200])):
                    try:
                        inner = da_stripped
                        if inner.lower().startswith("```json"):
                            inner = inner[7:].strip()
                        if inner.startswith("```"):
                            inner = inner[3:].lstrip()
                        if inner.endswith("```"):
                            inner = inner[:-3].rstrip()
                        inner_parsed = json.loads(inner)
                        if not isinstance(inner_parsed, dict):
                            raise ValueError("inner not dict")
                        # Case 1: inner is full AnswerCard at top level
                        inner_da = inner_parsed.get("direct_answer")
                        inner_secs = inner_parsed.get("sections")
                        if isinstance(inner_da, str) and isinstance(inner_secs, list) and not (
                            inner_da.strip().startswith("{") or inner_da.strip().startswith("```")
                        ):
                            mode = inner_parsed.get("mode") if inner_parsed.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED") else "FACTUAL"
                            sections_out = []
                            for s in inner_secs:
                                sec = dict(s) if isinstance(s, dict) else {}
                                if not sec.get("label") and sec.get("title"):
                                    sec["label"] = sec.get("title", "")
                                sections_out.append(sec)
                            display_message = json.dumps({"mode": mode, "direct_answer": inner_da, "sections": sections_out})
                        else:
                            # Case 2: inner has resolutions; extract from first resolution
                            res_list = inner_parsed.get("resolutions")
                            if isinstance(res_list, list) and len(res_list) > 0:
                                first = res_list[0]
                                res = first.get("resolution") if isinstance(first.get("resolution"), dict) else first
                                if isinstance(res, dict) and isinstance(res.get("direct_answer"), str) and isinstance(res.get("sections"), list):
                                    mode = res.get("mode") if res.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED") else "FACTUAL"
                                    sections_out = []
                                    for s in res["sections"]:
                                        sec = dict(s) if isinstance(s, dict) else {}
                                        if not sec.get("label") and sec.get("title"):
                                            sec["label"] = sec.get("title", "")
                                        sections_out.append(sec)
                                    display_message = json.dumps({"mode": mode, "direct_answer": res["direct_answer"], "sections": sections_out})
                                elif isinstance(first.get("resolution"), str):
                                    # resolution is plain text (schema: "answer text")
                                    mode = inner_parsed.get("mode") if inner_parsed.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED") else "FACTUAL"
                                    display_message = json.dumps({"mode": mode, "direct_answer": first["resolution"], "sections": []})
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass
                else:
                    # Normal AnswerCard
                    mode = parsed.get("mode") if parsed.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED") else "FACTUAL"
                    sections_out = []
                    for s in secs:
                        sec = dict(s) if isinstance(s, dict) else {}
                        if not sec.get("label") and sec.get("title"):
                            sec["label"] = sec.get("title", "")
                        sections_out.append(sec)
                    display_message = json.dumps({"mode": mode, "direct_answer": da, "sections": sections_out})
            elif parsed.get("resolutions"):
                # Top-level resolutions format; extract first for AnswerCard
                r = parsed.get("resolutions")
                if isinstance(r, list) and len(r) > 0:
                    first = r[0]
                    if isinstance(first, dict):
                        res = first.get("resolution") if isinstance(first.get("resolution"), dict) else first
                        if isinstance(res.get("direct_answer"), str) and isinstance(res.get("sections"), list):
                            mode = res.get("mode") if res.get("mode") in ("FACTUAL", "CANONICAL", "BLENDED") else "FACTUAL"
                            sections_out = []
                            for s in res["sections"]:
                                sec = dict(s) if isinstance(s, dict) else {}
                                if not sec.get("label") and sec.get("title"):
                                    sec["label"] = sec.get("title", "")
                                sections_out.append(sec)
                            display_message = json.dumps({"mode": mode, "direct_answer": res["direct_answer"], "sections": sections_out})
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
            r = parsed.get("resolutions")
            if isinstance(r, list):
                resolutions = [x for x in r if isinstance(x, dict)]
            v = parsed.get("closed_task_ids")
            if isinstance(v, list):
                closed_task_ids[:] = [str(x) for x in v if x]
            v = parsed.get("open_task_ids")
            if isinstance(v, list):
                open_task_ids[:] = [str(x) for x in v if x]
            ns = parsed.get("next_steps")
            if isinstance(ns, list):
                next_steps = [str(x) for x in ns if x]
            nq = parsed.get("next_questions_for_user")
            if isinstance(nq, list):
                next_questions_for_user = [str(x) for x in nq if x]
    except (json.JSONDecodeError, TypeError, ValueError):
        # Unparseable response (e.g. integrator exception → plain text): show try-again as AnswerCard
        _raw_truncated = (final_message or "")[:2000] + ("..." if len(final_message or "") > 2000 else "")
        logger.warning(
            "Integrate: could not parse final_message as JSON; sending try-again stub. raw (truncated): %s",
            _raw_truncated,
        )
        display_message = json.dumps({
            "mode": "FACTUAL",
            "direct_answer": FALLBACK_TRY_AGAIN,
            "sections": [],
        })

    # If we never produced valid AnswerCard JSON, show try-again so the card always formats
    try:
        check = json.loads(display_message) if display_message else {}
        if not isinstance(check, dict) or check.get("mode") not in ("FACTUAL", "CANONICAL", "BLENDED") or "direct_answer" not in check or not isinstance(check.get("sections"), list):
            _msg_truncated = (display_message or "")[:2000] + ("..." if len(display_message or "") > 2000 else "")
            logger.warning(
                "Integrate: display_message not valid AnswerCard; sending try-again stub. message (truncated): %s",
                _msg_truncated,
            )
            display_message = json.dumps({
                "mode": "FACTUAL",
                "direct_answer": FALLBACK_TRY_AGAIN,
                "sections": [],
            })
    except (json.JSONDecodeError, TypeError, ValueError):
        _msg_truncated = (display_message or "")[:2000] + ("..." if len(display_message or "") > 2000 else "")
        logger.warning(
            "Integrate: display_message not parseable; sending try-again stub. message (truncated): %s",
            _msg_truncated,
        )
        display_message = json.dumps({
            "mode": "FACTUAL",
            "direct_answer": FALLBACK_TRY_AGAIN,
            "sections": [],
        })

    # Deterministic: only accept task IDs that exist in the plan (upsert-only, no LLM-invented ids)
    valid_sq_ids = {str(sq.id) for sq in plan.subquestions} if plan and getattr(plan, "subquestions", None) else set()
    if valid_sq_ids:
        closed_task_ids[:] = [x for x in closed_task_ids if str(x) in valid_sq_ids]
        open_task_ids[:] = [x for x in open_task_ids if str(x) in valid_sq_ids]
        resolutions[:] = [r for r in resolutions if isinstance(r, dict) and str(r.get("sq_id", "")) in valid_sq_ids]

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

    payload = {
        "status": "completed",
        "message": display_message,
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
    if resolutions:
        payload["resolutions"] = resolutions
    if closed_task_ids:
        payload["closed_task_ids"] = closed_task_ids
    if open_task_ids:
        payload["open_task_ids"] = open_task_ids
    if next_steps:
        payload["next_steps"] = next_steps
    if next_questions_for_user:
        payload["next_questions_for_user"] = next_questions_for_user
    roster_step_outputs = getattr(ctx, "roster_step_outputs", None)
    if roster_step_outputs:
        payload["roster_step_outputs"] = roster_step_outputs
    roster_report_pdf = getattr(ctx, "roster_report_pdf_base64", None)
    roster_report_final_md = getattr(ctx, "roster_report_final_md", None)
    if roster_report_pdf and isinstance(roster_report_pdf, str) and len(roster_report_pdf) > 0:
        payload["roster_report_pdf_base64"] = roster_report_pdf
        logger.info("Roster payload: PDF included (%d bytes)", len(roster_report_pdf))
    if roster_report_final_md and isinstance(roster_report_final_md, str) and len(roster_report_final_md.strip()) > 0:
        payload["roster_report_final_md"] = roster_report_final_md
        has_charts = "data:image/png;base64," in roster_report_final_md
        logger.info("Roster payload: final_md included (%d chars, charts=%s)", len(roster_report_final_md), has_charts)
    ctx.response_payload = payload
