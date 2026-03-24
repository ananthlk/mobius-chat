"""
Three-path model for NPI/credentialing:

a) Answer from previous response: active_skill (or report_run_id) in context → reasoning or
   credentialing_qa answers from the existing report; no re-run.

b) NPI Q&A tool (credentialing_qa): generic credentialing questions (explain section E, what is
   section E, etc.) that are NOT "look up NPI", "build report", etc. → credentialing_qa path:
   answer from report if in context, else generic message (CREDENTIALING_QA_NO_REPORT).

c) Specific tools for specific requests: "create credentialing report for [Org]" → roster_report
   (11-step); "find NPI for X" → npi_lookup; etc.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.planner.blueprint import build_blueprint
from app.planner.schemas import Plan, SubQuestion
from app.services.tool_agent import (
    CREDENTIALING_QA_NO_REPORT,
    _is_plausible_org_name,
    answer_tool,
)
from app.services.doc_assembly import RETRIEVAL_SIGNAL_ROSTER_COMPLETE, RETRIEVAL_SIGNAL_NO_SOURCES


# ---- (c) Plausible org: only real org names trigger build report ----
def test_is_plausible_org_name_real_org():
    assert _is_plausible_org_name("David Lawrence Center") is True
    assert _is_plausible_org_name("Aspire Health") is True
    assert _is_plausible_org_name("Acme Corp") is True


def test_is_plausible_org_name_followup_not_org():
    assert _is_plausible_org_name("i meant section E of the credentialing report") is False
    assert _is_plausible_org_name("can you explain section E for me") is False
    assert _is_plausible_org_name("what does the report say about section C") is False
    assert _is_plausible_org_name("how many npi have pml issues") is False


# ---- (b) credentialing_qa: generic questions → tool_hint=credentialing_qa, not roster_report ----
def test_blueprint_explain_section_e_gets_credentialing_qa():
    """Generic 'explain section E' → credentialing_qa so we answer from report or generic, never run 11 steps."""
    plan = Plan(
        subquestions=[
            SubQuestion(
                id="sq1",
                text="can you explain section E for me",
                kind="tool",
                capabilities_primary="tools",
                tool_hint="roster_report",
            )
        ]
    )
    rctx = {"user_message": "can you explain section E for me"}
    blueprint = build_blueprint(plan, retrieval_ctx=rctx)
    assert len(blueprint) == 1
    assert blueprint[0]["agent"] == "tool"
    assert blueprint[0].get("tool_hint") == "credentialing_qa"


def test_blueprint_i_meant_section_e_gets_credentialing_qa():
    """'i meant section E of the credentialing report' must not be treated as build report for that string."""
    plan = Plan(
        subquestions=[
            SubQuestion(
                id="sq1",
                text="i meant section E of the credentialing report",
                kind="tool",
                capabilities_primary="tools",
                tool_hint="roster_report",
            )
        ]
    )
    rctx = {"user_message": "i meant section E of the credentialing report"}
    blueprint = build_blueprint(plan, retrieval_ctx=rctx)
    assert blueprint[0].get("tool_hint") == "credentialing_qa"


def test_blueprint_create_report_for_org_stays_roster_report():
    """Explicit 'create credentialing report for David Lawrence Center' → roster_report (build)."""
    plan = Plan(
        subquestions=[
            SubQuestion(
                id="sq1",
                text="Create a credentialing report for David Lawrence Center",
                kind="tool",
                capabilities_primary="tools",
                tool_hint="roster_report",
            )
        ]
    )
    rctx = {"user_message": "Create a credentialing report for David Lawrence Center"}
    blueprint = build_blueprint(plan, retrieval_ctx=rctx)
    assert blueprint[0]["agent"] == "tool"
    # Parser said roster_report; no follow-up phrase override → keep roster_report (build)
    assert blueprint[0].get("tool_hint") == "roster_report"


# ---- (b) credentialing_qa with no report → generic message ----
def test_credentialing_qa_no_report_returns_generic():
    """When tool_hint=credentialing_qa and no report in context, return CREDENTIALING_QA_NO_REPORT."""
    with patch.dict("os.environ", {"CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": "http://test:8011"}, clear=False):
        ans, sources, usage, signal = answer_tool(
            question="can you explain section E for me",
            user_message="can you explain section E for me",
            active_context={},
            tool_hint_override="credentialing_qa",
        )
    assert signal == RETRIEVAL_SIGNAL_NO_SOURCES
    assert "Create a credentialing report" in ans or "credentialing report" in ans.lower()
    assert "NPPES" in ans


# ---- (a) credentialing_qa with report in context → answer from report ----
def test_credentialing_qa_with_report_run_id_asks_report():
    """When credentialing_qa and report_run_id in context, call /ask and return answer from report."""
    with patch.dict("os.environ", {"CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": "http://test:8011"}, clear=False):
        with patch("app.services.tool_agent._ask_credentialing_report") as mock_ask:
            mock_ask.return_value = (
                "Section E is the rate gap (directional comparison to state benchmarks).",
                [{"document_name": "Credentialing report"}],
                None,
                RETRIEVAL_SIGNAL_ROSTER_COMPLETE,
            )
            ans, sources, usage, signal = answer_tool(
                question="explain section E",
                user_message="explain section E",
                active_context={"report_run_id": "run-123"},
                tool_hint_override="credentialing_qa",
            )
    mock_ask.assert_called_once()
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    assert "Section E" in ans
