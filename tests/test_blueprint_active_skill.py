"""
Tests for blueprint active_skill pre-check: follow-ups about an existing report
should get agent=reasoning and must NOT re-run the 11-step roster_report pipeline.
"""
from __future__ import annotations

import pytest

from app.planner.blueprint import build_blueprint, _message_refers_to_org
from app.planner.schemas import Plan, SubQuestion


def _plan_with_one_tool_subquestion(text: str = "Create a credentialing report for David Lawrence Center"):
    return Plan(
        subquestions=[
            SubQuestion(
                id="sq1",
                text=text,
                kind="tool",
                capabilities_primary="tools",
                tool_hint="roster_report",
            )
        ]
    )


def test_message_refers_to_org_full_name():
    assert _message_refers_to_org("Tell me more about the credentialing report for David Lawrence Center", "David Lawrence Center") is True
    assert _message_refers_to_org("credentialing report for David Lawrence Center", "David Lawrence Center") is True


def test_message_refers_to_org_first_two_words():
    assert _message_refers_to_org("Tell me more about the report for David Lawrence", "David Lawrence Center") is True


def test_message_refers_to_org_no_match():
    assert _message_refers_to_org("How many NPIs have PML issues?", "David Lawrence Center") is False
    assert _message_refers_to_org("Create a report for Acme Corp", "David Lawrence Center") is False


def test_blueprint_active_skill_precheck_uses_reasoning():
    """When active_skill is roster_report for an org and message refers to that org, agent=reasoning (no re-run)."""
    plan = _plan_with_one_tool_subquestion()
    retrieval_ctx = {
        "user_message": "Tell me more about the credentialing report for David Lawrence Center",
        "active_skill": {
            "skill": "roster_report",
            "org": "David Lawrence Center",
            "data": {"section_c_count": 45, "readiness_score": 46.07},
        },
    }
    blueprint = build_blueprint(plan, retrieval_ctx=retrieval_ctx)
    assert len(blueprint) == 1
    assert blueprint[0]["agent"] == "reasoning"
    assert blueprint[0].get("tool_hint") == "roster_report"


def test_blueprint_active_skill_partial_org_name():
    """First two words of org in message still counts as same-org follow-up."""
    plan = _plan_with_one_tool_subquestion()
    retrieval_ctx = {
        "user_message": "Tell me more about the credentialing report for David Lawrence",
        "active_skill": {
            "skill": "roster_report",
            "org": "David Lawrence Center",
            "data": {},
        },
    }
    blueprint = build_blueprint(plan, retrieval_ctx=retrieval_ctx)
    assert blueprint[0]["agent"] == "reasoning"


def test_blueprint_without_active_skill_still_routes_on_triggers():
    """Without active_skill, detect_route can still set agent=tool for credentialing phrases."""
    plan = _plan_with_one_tool_subquestion("Create a credentialing report for Acme")
    retrieval_ctx = {
        "user_message": "Create a credentialing report for Acme",
    }
    blueprint = build_blueprint(plan, retrieval_ctx=retrieval_ctx)
    # Should get tool from route triggers (credentialing report for X)
    assert blueprint[0]["agent"] == "tool"


def test_blueprint_npi_pml_question_routes_to_credentialing_qa():
    """When parser chooses credentialing_qa, blueprint honors it (tool, not reasoning)."""
    from app.planner.blueprint import build_blueprint
    from app.planner.schemas import Plan, SubQuestion

    sq = SubQuestion(
        id="t1",
        text="Is NPI 1927298609 set up for PML?",
        kind="tool",
        question_intent="factual",
        capabilities_primary="tools",
    )
    sq.tool_hint = "credentialing_qa"  # Parser matched capability
    plan = Plan(subquestions=[sq])
    retrieval_ctx = {
        "user_message": "Is NPI 1927298609 set up for PML?",
        "active_skill": {"skill": "roster_report", "org": "David Lawrence Center"},
        "report_run_id": "run-123",
        "last_report_org": "David Lawrence Center",
    }
    blueprint = build_blueprint(plan, rag_default_k=10, retrieval_ctx=retrieval_ctx)
    assert len(blueprint) == 1
    assert blueprint[0]["agent"] == "tool"
    assert blueprint[0]["tool_hint"] == "credentialing_qa"


def test_blueprint_active_skill_different_org_does_not_force_reasoning():
    """If message mentions a different org, we do not force reasoning (allow new report for that org)."""
    plan = _plan_with_one_tool_subquestion()
    retrieval_ctx = {
        "user_message": "Create a credentialing report for Acme Corp",
        "active_skill": {
            "skill": "roster_report",
            "org": "David Lawrence Center",
            "data": {},
        },
    }
    blueprint = build_blueprint(plan, retrieval_ctx=retrieval_ctx)
    # Acme Corp != David Lawrence Center → pre-check does not apply; route triggers may still give tool
    assert blueprint[0]["agent"] == "tool"
