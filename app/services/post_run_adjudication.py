"""
Post-run adjudication: after a completed chat response is published, run v2 full-context
adjudication (shared with eval), write per-stage quality_score on llm_calls, update router EMA,
optional adjudication_scores row, and merge qc_audit into the response (SSE/poll).

Sampling: MOBIUS_POST_RUN_ADJUDICATE_EVERY_N (default 1) runs the LLM adjudicator on every
completed turn. Set to 20 (for example) to adjudicate ~1 in 20 turns deterministically by
correlation_id to save cost at scale.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.communication.json_display_sanitize import plain_text_for_adjudication_from_chat_message
from app.pipeline.context import PipelineContext

logger = logging.getLogger(__name__)


def _tool_fired_from_log(thinking_lines: list[str]) -> str:
    for line in reversed(thinking_lines or []):
        low = line.lower()
        if "run_credentialing_report" in low:
            return "run_credentialing_report"
        if "search_corpus" in low:
            return "search_corpus"
        if "google_search" in low or "web_scrape" in low:
            return "google_search"
        if "refuse" in low and "tool" in low:
            return "refuse"
    return "unknown"


def _react_iterations(usage_breakdown: list[Any] | None) -> int:
    n = 0
    for r in usage_breakdown or []:
        if isinstance(r, dict) and str(r.get("stage") or "").startswith("react_"):
            n += 1
    return n


def _sub_scores_client(merged: dict[str, float | None]) -> dict[str, float]:
    return {k: float(v) for k, v in merged.items() if v is not None}


def _should_post_run_adjudicate_this_turn(correlation_id: str) -> bool:
    """When MOBIUS_POST_RUN_ADJUDICATE_EVERY_N > 1, run for ~1/N turns (stable hash of correlation_id)."""
    raw = (os.environ.get("MOBIUS_POST_RUN_ADJUDICATE_EVERY_N") or "1").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 1
    if n <= 1:
        return True
    try:
        u = uuid.UUID(str(correlation_id))
    except (ValueError, TypeError, AttributeError):
        logger.debug("post_run adjudication sampling: invalid correlation_id, adjudicating anyway")
        return True
    return (u.int % n) == 0


def schedule_post_run_adjudication(ctx: PipelineContext, payload: dict[str, Any]) -> None:
    """Fire-and-forget thread; does not block the worker."""
    raw = (os.environ.get("MOBIUS_POST_RUN_ADJUDICATE") or "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return
    if (payload or {}).get("status") != "completed":
        return
    cid = getattr(ctx, "correlation_id", None)
    if not cid:
        return
    if not _should_post_run_adjudicate_this_turn(str(cid)):
        logger.debug(
            "post_run adjudication skipped (MOBIUS_POST_RUN_ADJUDICATE_EVERY_N sample): cid=%s",
            str(cid)[:8],
        )
        return
    t = threading.Thread(
        target=_thread_main,
        args=(ctx, payload),
        daemon=True,
        name=f"post-adjudicate-{str(cid)[:8]}",
    )
    t.start()


def _thread_main(ctx: PipelineContext, payload: dict[str, Any]) -> None:
    try:
        asyncio.run(_run_async(ctx, payload))
    except Exception as e:
        logger.warning("post_run_adjudication failed: %s", e, exc_info=True)


async def _run_async(ctx: PipelineContext, payload: dict[str, Any]) -> None:
    from app.prompts_llm_config import load_prompts_llm_config
    from app.queue import get_queue
    from app.services.adjudication.full import adjudicate_full_async
    from app.services.adjudication.stage_meta import build_stage_metadata
    from app.services.llm_analytics import (
        fetch_quality_enrich_map_for_correlation_async,
        update_quality_for_correlation_stages_async,
    )
    from app.services.model_registry import get_router
    from app.storage.adjudication_scores import insert_adjudication_score_row
    from app.storage.progress import publish_quality_audit_event
    from app.storage.turns import fetch_turn_qc_audit, update_turn_qc_audit

    _, sha = load_prompts_llm_config()
    config_sha = sha or None
    question = (ctx.refined_query or ctx.message or "").strip()
    raw_assistant = (payload.get("message") or ctx.final_message or "").strip()
    # Wire format is often AnswerCard JSON; adjudicator prompt says "what the user saw" — use plain text.
    answer = plain_text_for_adjudication_from_chat_message(raw_assistant, max_chars=12000)
    thinking_lines = [str(x) for x in (payload.get("thinking_log") or getattr(ctx, "thinking_chunks", None) or [])]
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else []
    adjudication_sources = (
        payload.get("adjudication_sources")
        if isinstance(payload.get("adjudication_sources"), list)
        else []
    )
    sources_for_adj = adjudication_sources if adjudication_sources else sources
    usage_breakdown = payload.get("usage_breakdown") if isinstance(payload.get("usage_breakdown"), list) else []

    tool_fired = _tool_fired_from_log(thinking_lines)
    legacy_path = (os.environ.get("MOBIUS_USE_REACT") or "1").strip().lower() in ("0", "false", "no", "off")
    stage_meta = build_stage_metadata(
        thinking_log=thinking_lines,
        tool_fired=tool_fired,
        expected_tool=None,
        iterations=_react_iterations(usage_breakdown),
        legacy_path=legacy_path,
        usage_breakdown=usage_breakdown,
    )

    adj = await adjudicate_full_async(
        question=question,
        answer=answer,
        thinking_log=thinking_lines,
        sources=sources_for_adj,
        stage_metadata=stage_meta,
        usage_breakdown=usage_breakdown,
        use_chat_llm=True,
        correlation_id=ctx.correlation_id,
        thread_id=ctx.thread_id,
        config_sha=config_sha,
        user_profile=getattr(ctx, "user_profile", None),
    )

    verdict = str(adj.get("verdict") or "FAIL")
    passed = verdict in ("PASS", "PARTIAL")
    score = float(adj.get("overall_score") or 0.0)
    merged = adj.get("sub_scores") or {}
    reason = (adj.get("rationale") or "").strip()[:4000]
    raw_text = str(adj.get("adjudicator_raw_text") or "")[:8000]
    attr = adj.get("attribution") or {}

    stage_scores = adj.get("stage_scores") if isinstance(adj.get("stage_scores"), dict) else None
    await update_quality_for_correlation_stages_async(
        ctx.correlation_id,
        merged,
        score,
        "post_run_adjudicator_v2",
        stage_scores=stage_scores,
    )

    model_id = getattr(ctx, "integrator_model_id", None) or (payload.get("model_used") or "").strip() or None
    if model_id:
        try:
            get_router().observe_quality(model_id, score)
        except Exception as e:
            logger.debug("post_run observe_quality: %s", e)

    audited_at = datetime.now(timezone.utc).isoformat()
    adjud_usage = adj.get("adjudicator_usage") if isinstance(adj.get("adjudicator_usage"), dict) else {}
    adj_model = str(adjud_usage.get("model") or "").strip()
    adj_call = str(adjud_usage.get("llm_call_id") or "").strip()

    qc_dict: dict[str, Any] = {
        "passed": passed,
        "reason": reason or verdict,
        "source": "post_run_adjudicator",
        "audited_at": audited_at,
        "automated_score": round(score, 4),
        "sub_scores": _sub_scores_client(merged),
        "adjudicator_full_response": raw_text,
        "adjudication_verdict": verdict,
        "question_category": adj.get("question_category"),
        "adjudication_flags": adj.get("flags"),
    }
    if adj_model:
        qc_dict["adjudicator_model"] = adj_model
    if adj_call:
        qc_dict["adjudicator_llm_call_id"] = adj_call
    if stage_scores:
        qc_dict["stage_scores"] = stage_scores

    sym = "✓" if passed else "⚠"
    label = "passed" if passed else "flagged"
    reason_bit = f" — {reason[:180]}" if reason else ""
    line = f"{sym} Quality audit {label}{reason_bit}"

    # Optional analytics row (fails soft if migration not applied)
    try:
        ss = merged
        await insert_adjudication_score_row(
            {
                "correlation_id": ctx.correlation_id,
                "eval_run_id": None,
                "test_id": None,
                "question": question[:2000],
                "question_category": adj.get("question_category"),
                "tool_fired": stage_meta.get("tool_fired"),
                "expected_tool": stage_meta.get("expected_tool"),
                "planner_model": stage_meta.get("planner_model"),
                "rag_model": stage_meta.get("rag_model"),
                "integrator_model": stage_meta.get("integrator_model"),
                "badge_model": stage_meta.get("badge_model"),
                "jurisdiction": str(stage_meta.get("jurisdiction") or "")[:500],
                "iterations": stage_meta.get("iterations"),
                "legacy_path": stage_meta.get("legacy_path"),
                "addresses_question": ss.get("addresses_question"),
                "completeness": ss.get("completeness"),
                "factual_consistency": ss.get("factual_consistency"),
                "clarity": ss.get("clarity"),
                "actionability": ss.get("actionability"),
                "escalation_quality": ss.get("escalation_quality"),
                "language_quality": ss.get("language_quality"),
                "response_efficiency": ss.get("response_efficiency"),
                "json_compliance": ss.get("json_compliance"),
                "grounding": ss.get("grounding"),
                "confidence_calibration": ss.get("confidence_calibration"),
                "phi_boundary": ss.get("phi_boundary"),
                "clinical_boundary": ss.get("clinical_boundary"),
                "npi_accuracy": ss.get("npi_accuracy"),
                "org_match": ss.get("org_match"),
                "code_accuracy": ss.get("code_accuracy"),
                "payer_accuracy": ss.get("payer_accuracy"),
                "policy_currency": ss.get("policy_currency"),
                "enrollment_accuracy": ss.get("enrollment_accuracy"),
                "roster_accuracy": ss.get("roster_accuracy"),
                "data_freshness": ss.get("data_freshness"),
                "source_authority": ss.get("source_authority"),
                "context_accuracy": ss.get("context_accuracy"),
                "pronoun_resolution": ss.get("pronoun_resolution"),
                "overall_score": score,
                "verdict": verdict,
                "rationale": reason[:2000] if reason else None,
                "flags": adj.get("flags"),
                "failure_stage": attr.get("failure_stage"),
                "failure_reason": (attr.get("failure_reason") or "")[:1000] or None,
                "is_planner_fault": bool(attr.get("is_planner_fault")),
                "is_rag_fault": bool(attr.get("is_rag_fault")),
                "is_integrator_fault": bool(attr.get("is_integrator_fault")),
                "is_no_fault": bool(attr.get("is_no_fault")),
                "adjudicator_model": adj_model or None,
                "adjudicator_version": "v2",
                "used_llm": bool(adj.get("used_llm")),
                "used_heuristic": bool(adj.get("used_heuristic")),
            }
        )
    except Exception as e:
        logger.debug("post_run adjudication_scores insert skipped: %s", e)

    update_turn_qc_audit(ctx.correlation_id, qc_dict)
    try:
        qc_for_client = fetch_turn_qc_audit(ctx.correlation_id) or qc_dict
    except Exception:
        qc_for_client = qc_dict
    publish_quality_audit_event(ctx.correlation_id, {"passed": passed, "source": "post_run_adjudicator"}, line)
    adj_row = None
    if adjud_usage:
        try:
            from app.stages.integrate import breakdown_row_from_usage

            adj_row = breakdown_row_from_usage(dict(adjud_usage))
        except Exception as e:
            logger.debug("post_run: breakdown_row_from_usage skipped: %s", e)
    from app.communication.emit_envelope import make_answer_quality
    _aq_envelope = make_answer_quality(
        ctx.correlation_id,
        verdict=verdict,
        score=score,
        sub_scores=_sub_scores_client(merged),
        failure_stage=attr.get("failure_stage"),
        thread_id=ctx.thread_id,
    )
    merge_updates: dict[str, Any] = {"qc_audit": qc_for_client, "thinking_log": [line, _aq_envelope.to_dict()]}
    enrich: dict[str, Any] = {}
    try:
        enrich = await fetch_quality_enrich_map_for_correlation_async(ctx.correlation_id)
    except Exception as e:
        logger.debug("post_run fetch_quality_enrich_map: %s", e)
    if enrich:
        merge_updates["usage_breakdown_enrich"] = enrich
    if adj_row:
        ac = str(adj_row.get("llm_call_id") or "").strip()
        if ac and ac in enrich and isinstance(enrich[ac], dict):
            adj_row = {**adj_row, **enrich[ac]}
        merge_updates["usage_breakdown_append"] = [adj_row]
    get_queue().patch_response_merge(ctx.correlation_id, merge_updates)
    logger.info(
        "post_run_adjudication done cid=%s verdict=%s score=%.2f",
        str(ctx.correlation_id)[:8],
        verdict,
        score,
    )
