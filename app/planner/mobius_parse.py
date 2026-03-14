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
    def _list(v: Any) -> list:
        return [str(x) for x in v if x] if isinstance(v, list) else []
    return JurisdictionInfo(
        needed=bool(d.get("needed", False)),
        required_fields=_list(d.get("required_fields")),
        blocking_if_missing=_list(d.get("blocking_if_missing")),
        can_default=_list(d.get("can_default")),
        notes=str(d.get("notes") or ""),
    )


def _parse_capabilities(d: Any) -> CapabilitiesNeeded:
    if not isinstance(d, dict):
        return CapabilitiesNeeded()
    primary_raw = (d.get("primary") or "rag").lower()
    # Map LLM variants (web_scrape, google_search, search) to schema value "web"
    primary_map = {"web_scrape": "web", "google_search": "web", "search": "web"}
    primary = primary_map.get(primary_raw, primary_raw)
    if primary not in ("rag", "tools", "web", "reasoning", "ask_user", "refuse"):
        primary = "rag"
    fallbacks_raw = d.get("fallbacks") or []
    valid = ("rag", "tools", "web", "reasoning", "ask_user", "refuse")
    fallbacks: list[str] = []
    for x in fallbacks_raw:
        if isinstance(x, dict):
            # LLM may return {"if": "no_evidence", "then": "web"} - extract "then"
            then_val = (x.get("then") or "").lower()
            then_val = primary_map.get(then_val, then_val)
            if then_val in valid:
                fallbacks.append(then_val)
        elif isinstance(x, str) and x:
            v = primary_map.get(x.lower(), x.lower())
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
    pre_answer = d.get("pre_answer")
    pre_answer = str(pre_answer).strip() if pre_answer and isinstance(pre_answer, str) else None
    if not pre_answer:
        pre_answer = None

    # Extract tool_hint (may be in the task dict at top level)
    tool_hint_raw = d.get("tool_hint")
    tool_hint = str(tool_hint_raw).strip().lower() if tool_hint_raw and str(tool_hint_raw).lower() not in ("null", "none", "") else None

    # Extract skip_layer_4
    skip_layer_4_raw = d.get("skip_layer_4")
    if isinstance(skip_layer_4_raw, bool):
        skip_layer_4 = skip_layer_4_raw
    elif isinstance(skip_layer_4_raw, str):
        skip_layer_4 = skip_layer_4_raw.lower() in ("true", "1", "yes")
    else:
        skip_layer_4 = False

    return TaskPlanSubQuestion(
        id=str(sq_id),
        text=str(text).strip(),
        kind=kind,
        question_intent=intent,
        intent_score=score,
        jurisdiction=_parse_jurisdiction(d.get("jurisdiction")),
        capabilities_needed=_parse_capabilities(d.get("capabilities_needed")),
        tool_hint=tool_hint,
        skip_layer_4=skip_layer_4,
        pre_answer=pre_answer,
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


def _parse_task_as_subquestion(d: Any, index: int) -> TaskPlanSubQuestion | None:
    """Parse a task from the tasks-based schema (task_id, subquestion, kind, ...) into TaskPlanSubQuestion."""
    if not isinstance(d, dict):
        return None
    text = d.get("subquestion") or d.get("text") or ""
    if not text:
        return None
    sq_id = d.get("task_id") or d.get("id") or f"sq{index + 1}"
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
    # New schema: jurisdiction has state, payer, program (values); adapt to JurisdictionInfo
    jd = d.get("jurisdiction") or {}
    j_needed = bool(
        (jd.get("state") or "").strip()
        or (jd.get("payer") or "").strip()
        or (jd.get("program") or "").strip()
    )
    jurisdiction = JurisdictionInfo(needed=j_needed)
    capabilities_needed = _parse_capabilities(d.get("capabilities_needed"))

    # Extract tool_hint and skip_layer_4 from the task dict
    tool_hint_raw = d.get("tool_hint")
    tool_hint = str(tool_hint_raw).strip().lower() if tool_hint_raw and str(tool_hint_raw).lower() not in ("null", "none", "") else None

    skip_layer_4_raw = d.get("skip_layer_4")
    if isinstance(skip_layer_4_raw, bool):
        skip_layer_4 = skip_layer_4_raw
    elif isinstance(skip_layer_4_raw, str):
        skip_layer_4 = skip_layer_4_raw.lower() in ("true", "1", "yes")
    else:
        skip_layer_4 = False

    return TaskPlanSubQuestion(
        id=str(sq_id),
        text=str(text).strip(),
        kind=kind,
        question_intent=intent,
        intent_score=score,
        jurisdiction=jurisdiction,
        capabilities_needed=capabilities_needed,
        tool_hint=tool_hint,
        skip_layer_4=skip_layer_4,
        pre_answer=None,
    )


def _parse_task_from_task_schema(d: Any, index: int) -> TaskItem | None:
    """Parse a task from tasks-based schema into TaskItem (id, subquestion_id, modality)."""
    if not isinstance(d, dict):
        return None
    task_id = d.get("task_id") or d.get("id") or f"t{index + 1}"
    sq_id = task_id  # same id links TaskItem to TaskPlanSubQuestion
    caps = d.get("capabilities_needed") or {}
    primary_raw = (caps.get("primary") or "rag").lower()
    primary_map = {"web_scrape": "web", "google_search": "web", "search": "web"}
    modality = primary_map.get(primary_raw, primary_raw)
    if modality not in ("rag", "tools", "web", "reasoning", "ask_user", "refuse", "synthesize"):
        modality = "rag"
    fallbacks_raw = caps.get("fallbacks") or []
    fallbacks: list[TaskFallback] = []
    for x in fallbacks_raw:
        if isinstance(x, str) and x:
            fallbacks.append(TaskFallback(if_condition="no_evidence", then=x))
        elif isinstance(x, dict) and x.get("then"):
            fallbacks.append(_parse_fallback(x))
    return TaskItem(
        id=str(task_id),
        subquestion_id=str(sq_id),
        modality=modality,
        goal="",
        inputs=TaskInputs(),
        fallbacks=fallbacks,
    )


def parse_task_plan_from_json(raw: str) -> TaskPlan | None:
    """Parse TaskPlan from LLM JSON string. Supports both subquestions-based and tasks-based schemas."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if "{" not in text:
        return None
    # Accept either "subquestions" or "tasks" as the main list
    if "subquestions" not in text and "tasks" not in text:
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

    subquestions: list[TaskPlanSubQuestion] = []
    tasks: list[TaskItem] = []
    message_summary = ""

    # New schema: tasks with task_id + subquestion (question text in each task)
    # Legacy schema: subquestions (text there) + tasks (id, subquestion_id, modality)
    tasks_raw = data.get("tasks")
    sqs_raw = data.get("subquestions")
    first_task = tasks_raw[0] if (tasks_raw and isinstance(tasks_raw, list) and len(tasks_raw) > 0) else {}
    is_new_tasks_schema = (
        isinstance(first_task, dict)
        and ("subquestion" in first_task or "task_id" in first_task)
    )

    if tasks_raw and isinstance(tasks_raw, list) and is_new_tasks_schema:
        message_summary = str(data.get("plan_summary") or "").strip()
        for i, item in enumerate(tasks_raw):
            sq = _parse_task_as_subquestion(item, i)
            if sq:
                subquestions.append(sq)
            t = _parse_task_from_task_schema(item, i)
            if t:
                # Ensure task subquestion_id matches subquestion id
                if sq:
                    t = TaskItem(
                        id=t.id,
                        subquestion_id=sq.id,
                        modality=t.modality,
                        goal=t.goal,
                        inputs=t.inputs,
                        fallbacks=t.fallbacks,
                    )
                tasks.append(t)
    else:
        # Legacy schema: subquestions
        sqs_raw = data.get("subquestions") or []
        if not sqs_raw or not isinstance(sqs_raw, list):
            return None
        message_summary = str(data.get("message_summary") or "").strip()
        for i, item in enumerate(sqs_raw):
            sq = _parse_subquestion(item, i)
            if sq:
                subquestions.append(sq)
        for t in (data.get("tasks") or []):
            task = _parse_task(t)
            if task:
                tasks.append(task)

    if not subquestions:
        return None
    clarifications: list[ClarificationItem] = []
    for c in (data.get("clarifications") or []):
        cl = _parse_clarification(c)
        if cl:
            clarifications.append(cl)
    retry_d = data.get("retry_policy") or {}
    safety_d = data.get("safety") or {}
    nq_raw = data.get("next_questions_for_user") or []
    next_questions = [str(x) for x in nq_raw if x] if isinstance(nq_raw, list) else []
    msg_summary = str(data.get("message_summary") or data.get("plan_summary") or "")
    return TaskPlan(
        message_summary=msg_summary,
        subquestions=subquestions,
        clarifications=clarifications,
        tasks=tasks,
        next_questions_for_user=next_questions,
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
