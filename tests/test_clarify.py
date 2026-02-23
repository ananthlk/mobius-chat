"""Tests for clarify stage: jurisdiction, route clash, and refinement (prompt-driven skip)."""
from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.planner.schemas import Plan, SubQuestion
from app.stages.clarify import run_clarify


def _plan_with_task_plan(subquestions: list[dict]) -> Plan:
    """Build a Plan with task_plan set (Mobius planner output)."""
    sqs = [
        SubQuestion(id=s["id"], text=s["text"], kind=s.get("kind", "non_patient"))
        for s in subquestions
    ]
    plan = Plan(subquestions=sqs)
    # Mobius plans have task_plan; use a minimal dict to signal "from Mobius"
    plan.task_plan = type("TaskPlan", (), {"subquestions": sqs})()
    return plan


def _plan_legacy(subquestions: list[dict]) -> Plan:
    """Build a Plan without task_plan (legacy parser)."""
    sqs = [
        SubQuestion(id=s["id"], text=s["text"], kind=s.get("kind", "non_patient"))
        for s in subquestions
    ]
    return Plan(subquestions=sqs)


def test_mobius_plan_with_three_subquestions_skips_refinement():
    """Mobius plan with 3+ subquestions does not trigger refinement (prompt-driven)."""
    plan = _plan_with_task_plan([
        {"id": "sq1", "text": "ICD code for Socio Psycho Rehab"},
        {"id": "sq2", "text": "Is it covered under Medicaid in Florida"},
        {"id": "sq3", "text": "Does Sunshine Health require prior authorization"},
    ])
    ctx = PipelineContext(correlation_id="test", thread_id=None, message="ICD code, coverage, prior auth")
    ctx.plan = plan
    ctx.merged_state = {"active": {"payer": "Sunshine Health", "jurisdiction": "Florida"}}
    ctx.effective_message = ctx.message

    with patch("app.stages.clarify._get_rag_url", return_value=""):
        resolvable = run_clarify(ctx, emitter=None)

    assert resolvable is True
    assert ctx.should_refine is False


def test_legacy_plan_with_three_subquestions_still_refines_without_scenario():
    """Legacy plan (no task_plan) with 3 subquestions triggers refinement when no concrete scenario."""
    plan = _plan_legacy([
        {"id": "sq1", "text": "Medicaid eligibility"},
        {"id": "sq2", "text": "Health plans in Tampa"},
        {"id": "sq3", "text": "Enrollment process"},
    ])
    ctx = PipelineContext(correlation_id="test", thread_id=None, message="What is Medicaid and how to enroll?")
    ctx.plan = plan
    ctx.merged_state = {"active": {"payer": "Sunshine Health", "jurisdiction": "Florida"}}
    ctx.effective_message = ctx.message

    with patch("app.stages.clarify._get_rag_url", return_value=""):
        resolvable = run_clarify(ctx, emitter=None)

    # Legacy path: need_query_refinement runs; 3 subquestions + no concrete scenario -> refine
    assert resolvable is False
    assert ctx.should_refine is True
    assert len(ctx.refinement_suggestions) >= 1
