"""Parse Mobius TaskPlan from LLM JSON output. Lenient parsing for LLM variability."""
import json
import logging
from typing import Any

from app.planner.schemas import (
    TaskPlan,
    TaskPlanSubQuestion,
    JurisdictionInfo,
    CapabilitiesNeeded,
    ClarificationItem,
    TaskItem,
    TaskInputs,
    TaskFallback,
    RetryPolicy,
    SafetyInfo,
)

logger = logging.getLogger(__name__)


def _parse_jurisdiction(d: Any) -> JurisdictionInfo:
    if not isinstance(d, dict):
        return JurisdictionInfo()
    return JurisdictionInfo(
        needed=bool(d.get("needed", False)),
        required_fields=[str(x) for x in (d.get("required_fields") or []) if x],
        blocking_if_missing=[str(x) for x in (d.get("blocking_if_missing") or []) if x],
        can_default=[str(x) for x in (d.get("can_default") or []) if x],
        notes=str(d.get("notes") or ""),
    )


def _parse_capabilities(d: Any) -> CapabilitiesNeeded:
    if not isinstance(d, dict):
        return CapabilitiesNeeded()
    primary = (d.get("primary") or "rag").lower()
    if primary not in ("rag", "tools", "web", "reasoning", "ask_user", "refuse"):
        primary = "rag"
    fallbacks_raw = d.get("fallbacks") or []
    valid = ("rag", "tools", "web", "reasoning", "ask_user", "refuse")
    fallbacks: list[str] = []
    for x in fallbacks_raw:
        if isinstance(x, dict):
            # LLM may return {"if": "no_evidence", "then": "web"} - extract "then"
            then_val = (x.get("then") or "").lower()
            if then_val in valid:
                fallbacks.append(then_val)
        elif isinstance(x, str) and x:
            v = x.lower()
            if v in valid:
                fallbacks.append(v)
    return CapabilitiesNeeded(primary=primary, fallbacks=fallbacks)


def _parse_fallback(d: Any) -> TaskFallback:
    if not isinstance(d, dict):
        return TaskFallback()
    if_cond = d.get("if") or d.get("if_condition") or ""
    then_val = d.get("then") or ""
    return TaskFallback(if_condition=str(if_cond), then=str(then_val))


def _parse_task(d: Any) -> TaskItem | None:
    if not isinstance(d, dict) or not d.get("id") or not d.get("subquestion_id"):
        return None
    inputs_d = d.get("inputs") or {}
    fallbacks_raw = d.get("fallbacks") or []
    modality_raw = (d.get("modality") or "rag").lower()
    # Map LLM variants to schema values (web_scrape -> web, etc.)
    modality_map = {"web_scrape": "web", "google_search": "web", "search": "web"}
    modality = modality_map.get(modality_raw, modality_raw)
    if modality not in ("rag", "tools", "web", "reasoning", "ask_user", "refuse", "synthesize"):
        modality = "rag"
    return TaskItem(
        id=str(d["id"]),
        subquestion_id=str(d["subquestion_id"]),
        modality=modality,
        goal=str(d.get("goal") or ""),
        inputs=TaskInputs(
            rag_scopes=[str(x) for x in (inputs_d.get("rag_scopes") or []) if x],
            tool_capabilities=[str(x) for x in (inputs_d.get("tool_capabilities") or []) if x],
            web=dict(inputs_d.get("web")) if isinstance(inputs_d.get("web"), dict) else {},
            jurisdiction_fields_expected=[str(x) for x in (inputs_d.get("jurisdiction_fields_expected") or []) if x],
        ),
        fallbacks=[_parse_fallback(fb) for fb in fallbacks_raw if fb],
    )


def _parse_subquestion(d: Any, index: int) -> TaskPlanSubQuestion | None:
    if not isinstance(d, dict):
        return None
    text = d.get("text") or ""
    if not text:
        return None
    sq_id = d.get("id") or f"sq{index + 1}"
    kind_raw = (d.get("kind") or "non_patient").lower()
    kind = "non_patient"
    if kind_raw in ("patient", "non_patient", "tool"):
        kind = kind_raw
    intent_raw = (d.get("question_intent") or "factual").lower()
    intent = "factual"
    for v in ("factual", "canonical", "procedural", "diagnostic", "creative"):
        if intent_raw == v:
            intent = v
            break
    try:
        score = float(d.get("intent_score", 0.5))
    except (TypeError, ValueError):
        score = 0.5
    score = max(0.0, min(1.0, score))
    return TaskPlanSubQuestion(
        id=str(sq_id),
        text=str(text).strip(),
        kind=kind,
        question_intent=intent,
        intent_score=score,
        jurisdiction=_parse_jurisdiction(d.get("jurisdiction")),
        capabilities_needed=_parse_capabilities(d.get("capabilities_needed")),
    )


def _parse_clarification(d: Any) -> ClarificationItem | None:
    if not isinstance(d, dict) or not d.get("id") or not d.get("question"):
        return None
    return ClarificationItem(
        id=str(d["id"]),
        subquestion_id=str(d.get("subquestion_id") or ""),
        question=str(d.get("question") or ""),
        why_needed=str(d.get("why_needed") or ""),
        blocking=bool(d.get("blocking", True)),
        fills=[str(x) for x in (d.get("fills") or []) if x],
    )


def parse_task_plan_from_json(raw: str) -> TaskPlan | None:
    """Parse TaskPlan from LLM JSON string. Returns None on failure."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if "{" not in text or "subquestions" not in text:
        return None
    if "```" in text:
        start = text.find("```")
        if start >= 0:
            start = text.find("\n", start) + 1
            end = text.find("```", start)
            if end > start:
                text = text[start:end]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("[mobius_parse] JSON decode error: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    sqs_raw = data.get("subquestions") or []
    if not sqs_raw or not isinstance(sqs_raw, list):
        return None
    subquestions: list[TaskPlanSubQuestion] = []
    for i, item in enumerate(sqs_raw):
        sq = _parse_subquestion(item, i)
        if sq:
            subquestions.append(sq)
    if not subquestions:
        return None
    tasks: list[TaskItem] = []
    for t in (data.get("tasks") or []):
        task = _parse_task(t)
        if task:
            tasks.append(task)
    clarifications: list[ClarificationItem] = []
    for c in (data.get("clarifications") or []):
        cl = _parse_clarification(c)
        if cl:
            clarifications.append(cl)
    retry_d = data.get("retry_policy") or {}
    safety_d = data.get("safety") or {}
    return TaskPlan(
        message_summary=str(data.get("message_summary") or ""),
        subquestions=subquestions,
        clarifications=clarifications,
        tasks=tasks,
        retry_policy=RetryPolicy(
            max_attempts=int(retry_d.get("max_attempts", 2)),
            on_missing_jurisdiction=str(retry_d.get("on_missing_jurisdiction") or "ask_blocking_clarification"),
            on_no_results=str(retry_d.get("on_no_results") or "broaden_scope_then_offer_alternatives"),
            on_tool_error=str(retry_d.get("on_tool_error") or "simplify_then_fail_gracefully"),
        ),
        safety=SafetyInfo(
            contains_patient_request=bool(safety_d.get("contains_patient_request", False)),
            phi_risk=(safety_d.get("phi_risk") or "low").lower(),
            refusal_needed=bool(safety_d.get("refusal_needed", False)),
            notes=str(safety_d.get("notes") or ""),
        ),
    )
