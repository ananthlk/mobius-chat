"""Adapt Mobius TaskPlan to legacy Plan and blueprint format for pipeline compatibility."""
from typing import Any

from app.planner.schemas import (
    Plan,
    SubQuestion,
    TaskPlan,
    QuestionIntent,
)


def _intent_to_legacy(question_intent: str) -> QuestionIntent | None:
    """Map extended intents (procedural, diagnostic, creative) to factual/canonical."""
    if not question_intent:
        return None
    intent = (question_intent or "").strip().lower()
    if intent in ("factual", "canonical"):
        return intent
    if intent in ("procedural", "diagnostic"):
        return "canonical"
    if intent == "creative":
        return "factual"
    return None


def _get_on_rag_fail(task: Any, _sq_id: str) -> list[str]:
    """Extract on_rag_fail from task fallbacks: no_evidence/no_results -> web/search_google."""
    out: list[str] = []
    fallbacks = getattr(task, "fallbacks", []) or []
    for fb in fallbacks:
        cond = getattr(fb, "if_condition", None) or (fb.get("if") if isinstance(fb, dict) else "")
        then_val = getattr(fb, "then", None) or (fb.get("then") if isinstance(fb, dict) else "")
        if not cond or not then_val:
            continue
        cond_lower = str(cond).lower()
        then_lower = str(then_val).lower()
        if "no_evidence" in cond_lower or "no_results" in cond_lower:
            if "web" in then_lower or "search" in then_lower or "google" in then_lower:
                out.append("search_google")
    return out


def task_plan_to_plan(
    task_plan: TaskPlan,
    thinking_log: list[str],
    llm_usage: dict[str, Any] | None = None,
) -> Plan:
    """Convert Mobius TaskPlan to legacy Plan. Pipeline consumes Plan."""
    subquestions: list[SubQuestion] = []
    tasks_by_sq: dict[str, Any] = {t.subquestion_id: t for t in (task_plan.tasks or [])}

    for sq in task_plan.subquestions or []:
        task = tasks_by_sq.get(sq.id)
        intent = _intent_to_legacy(getattr(sq, "question_intent", "") or "factual")
        if intent is None:
            intent = "factual"
        intent_score = float(getattr(sq, "intent_score", 0.5))
        intent_score = max(0.0, min(1.0, intent_score))

        jurisdiction = getattr(sq, "jurisdiction", None)
        requires_jurisdiction: bool | None = None
        if jurisdiction is not None:
            requires_jurisdiction = getattr(jurisdiction, "needed", None)
            if requires_jurisdiction is None and hasattr(jurisdiction, "required_fields"):
                requires_jurisdiction = bool(getattr(jurisdiction, "required_fields", []))

        on_rag_fail: list[str] = []
        if task:
            on_rag_fail = _get_on_rag_fail(task, sq.id)
        if not on_rag_fail:
            caps = getattr(sq, "capabilities_needed", None)
            if caps:
                fallbacks = getattr(caps, "fallbacks", []) or []
                if "web" in fallbacks:
                    on_rag_fail = ["search_google"]

        caps = getattr(sq, "capabilities_needed", None)
        capabilities_primary = None
        if caps:
            capabilities_primary = str(getattr(caps, "primary", "") or "").strip().lower() or None

        subquestions.append(
            SubQuestion(
                id=sq.id,
                text=sq.text,
                kind=sq.kind,
                question_intent=intent,
                intent_score=intent_score,
                requires_jurisdiction=requires_jurisdiction,
                on_rag_fail=on_rag_fail,
                capabilities_primary=capabilities_primary,
            )
        )

    return Plan(
        subquestions=subquestions,
        thinking_log=thinking_log,
        llm_usage=llm_usage,
        task_plan=task_plan,
    )
