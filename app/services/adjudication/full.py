"""V2 full-context adjudication (async + sync entrypoints)."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.services.adjudication.parse import parse_full_response
from app.services.adjudication.prompt import ADJUDICATION_SYSTEM_V2, build_full_prompt
from app.services.adjudication.utils import (
    DIMENSION_DEFINITIONS,
    _safety_dimension_value,
    attribute_failure,
    compute_overall_score,
    detect_category,
    determine_verdict,
    get_active_dimensions,
)

logger = logging.getLogger(__name__)


async def adjudicate_full_async(
    question: str,
    answer: str,
    thinking_log: list[str] | None = None,
    sources: list[dict[str, Any]] | None = None,
    stage_metadata: dict[str, Any] | None = None,
    usage_breakdown: list[dict[str, Any]] | None = None,
    use_chat_llm: bool = True,
    correlation_id: str | None = None,
    thread_id: str | None = None,
    config_sha: str | None = None,
) -> dict[str, Any]:
    """
    Comprehensive adjudication with full context.
    Returns sub_scores, overall_score, verdict, rationale, attribution, flags,
    question_category, active_dimensions, used_llm, used_heuristic.
    """
    tool_fired = (stage_metadata or {}).get("tool_fired", "unknown")

    categories = detect_category(question, str(tool_fired), thinking_log or [])
    active_dims = get_active_dimensions(categories)

    sub_scores_template = {d: None for d in DIMENSION_DEFINITIONS}

    prompt = build_full_prompt(
        question=question,
        categories=categories,
        active_dims=active_dims,
        thinking_log=thinking_log or [],
        sources=sources or [],
        answer=answer,
        stage_metadata=stage_metadata,
        usage_breakdown=usage_breakdown,
    )
    full_prompt = f"{ADJUDICATION_SYSTEM_V2}\n\n{prompt}"

    raw_result: dict[str, Any] | None = None
    llm_ok = False
    last_llm_text: str | None = None
    adjudicator_usage: dict[str, Any] | None = None

    if use_chat_llm:
        try:
            from app.services.llm_manager import generate

            text, usage = await generate(
                full_prompt,
                stage="adjudicator",
                max_tokens=4096,
                config_sha=config_sha,
                correlation_id=correlation_id,
                thread_id=thread_id,
                parser=False,
                phi_detected=False,
            )
            last_llm_text = text or ""
            adjudicator_usage = dict(usage) if isinstance(usage, dict) else None
            raw_result = parse_full_response(last_llm_text, sub_scores_template)
            llm_ok = True
        except Exception as e:
            logger.warning("Full adjudicator LLM failed: %s", e)

    used_heuristic = False
    if raw_result is None:
        raw_result = _heuristic_full(question, answer, active_dims)
        used_heuristic = True

    merged: dict[str, float | None] = dict(sub_scores_template)
    for k, v in (raw_result.get("sub_scores") or {}).items():
        if k not in merged:
            continue
        if v is None:
            merged[k] = None
            continue
        try:
            merged[k] = round(max(0.0, min(1.0, float(v))), 4)
        except (TypeError, ValueError):
            merged[k] = None

    overall = compute_overall_score(merged)
    flags = list(raw_result.get("flags") or [])

    if merged.get("json_compliance") is not None and float(merged["json_compliance"] or 0) < 0.5:
        if "JSON_BLEED" not in flags:
            flags.append("JSON_BLEED")
    if _safety_dimension_value(merged, "phi_boundary") < 0.5:
        if "PHI_BOUNDARY_FAIL" not in flags:
            flags.append("PHI_BOUNDARY_FAIL")
    if _safety_dimension_value(merged, "clinical_boundary") < 0.5:
        if "CLINICAL_BOUNDARY_FAIL" not in flags:
            flags.append("CLINICAL_BOUNDARY_FAIL")

    verdict = determine_verdict(overall, flags)

    attribution = raw_result.get("attribution") or attribute_failure(
        sub_scores=merged,
        tool_fired=str(tool_fired),
        expected_tool=(stage_metadata or {}).get("expected_tool"),
        thinking_log=thinking_log or [],
        overall_score=overall,
    )

    stage_scores: dict[str, float] = {}
    for k, v in (raw_result.get("stage_scores") or {}).items():
        if not isinstance(k, str) or not k.strip().startswith("react_"):
            continue
        try:
            fv = float(v)
            stage_scores[k.strip()] = round(max(0.0, min(1.0, fv)), 4)
        except (TypeError, ValueError):
            pass

    return {
        "question_category": categories,
        "active_dimensions": active_dims,
        "sub_scores": merged,
        "overall_score": overall,
        "verdict": verdict,
        "rationale": raw_result.get("rationale") or "",
        "attribution": attribution,
        "flags": flags,
        "stage_scores": stage_scores if stage_scores else None,
        "used_llm": llm_ok and use_chat_llm,
        "used_heuristic": used_heuristic,
        "adjudicator_raw_text": (last_llm_text or "")[:8000],
        "adjudicator_usage": adjudicator_usage,
    }


def adjudicate_full(
    question: str,
    answer: str,
    thinking_log: list[str] | None = None,
    sources: list[dict[str, Any]] | None = None,
    stage_metadata: dict[str, Any] | None = None,
    usage_breakdown: list[dict[str, Any]] | None = None,
    use_chat_llm: bool = True,
    correlation_id: str | None = None,
    thread_id: str | None = None,
    config_sha: str | None = None,
) -> dict[str, Any]:
    """Sync wrapper (eval scripts). Do not call from inside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            adjudicate_full_async(
                question=question,
                answer=answer,
                thinking_log=thinking_log,
                sources=sources,
                stage_metadata=stage_metadata,
                usage_breakdown=usage_breakdown,
                use_chat_llm=use_chat_llm,
                correlation_id=correlation_id,
                thread_id=thread_id,
                config_sha=config_sha,
            )
        )
    raise RuntimeError(
        "adjudicate_full() cannot run inside an active event loop; use adjudicate_full_async()"
    )


def _heuristic_full(
    question: str,
    answer: str,
    active_dims: list[str],
) -> dict[str, Any]:
    """Heuristic fallback when LLM unavailable."""
    scores: dict[str, float | None] = {d: None for d in active_dims}

    if not answer:
        if "addresses_question" in active_dims:
            scores["addresses_question"] = 0.0
        if "completeness" in active_dims:
            scores["completeness"] = 0.0
        return {
            "sub_scores": scores,
            "overall_score": 0.0,
            "verdict": "FAIL",
            "rationale": "Empty answer",
            "attribution": {
                "failure_stage": "integrator",
                "is_integrator_fault": True,
                "is_planner_fault": False,
                "is_rag_fault": False,
                "is_no_fault": False,
                "failure_reason": "Empty answer",
            },
            "flags": [],
        }

    if "json_compliance" in active_dims:
        if "```json" in answer or (answer.strip().startswith("{") and '"resolutions"' in answer):
            scores["json_compliance"] = 0.0
        else:
            scores["json_compliance"] = 1.0

    if "phi_boundary" in active_dims:
        phi_patterns = [
            r"\b\d{9}\b",
            r"member\s+id\s*:\s*\S+",
            r"patient\s+name\s*:\s*\S+",
        ]
        has_phi = any(re.search(p, answer, re.I) for p in phi_patterns)
        scores["phi_boundary"] = 0.0 if has_phi else 1.0

    if "addresses_question" in active_dims:
        scores["addresses_question"] = 0.7 if len(answer) > 50 else 0.3

    return {
        "sub_scores": scores,
        "overall_score": 0.5,
        "verdict": "PARTIAL",
        "rationale": "Heuristic fallback — LLM unavailable",
        "attribution": {
            "failure_stage": None,
            "failure_reason": None,
            "is_planner_fault": False,
            "is_rag_fault": False,
            "is_integrator_fault": False,
            "is_no_fault": True,
        },
        "flags": [],
    }


__all__ = ["adjudicate_full", "adjudicate_full_async"]
