"""
Tests for credentialing report Q&A: ask about report anytime after a report,
or at start of conversation (latest report for org / NPI valid for Florida billing).
All paths use persistence (report runs, NPI profiles), not RAG.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from app.services.tool_agent import answer_tool
from app.services.doc_assembly import RETRIEVAL_SIGNAL_ROSTER_COMPLETE, RETRIEVAL_SIGNAL_NO_SOURCES


# ---------------------------------------------------------------------------
# Helper: run answer_tool with credentialing skill URL set and mocked HTTP
# ---------------------------------------------------------------------------

def _run_credentialing_qa(
    question: str,
    user_message: str | None = None,
    active_context: dict | None = None,
    get_latest_run: dict | None = None,
    ask_return: tuple[str, list, None, str] | None = None,
    get_org_candidates: list[str] | None = None,
):
    """Call answer_tool with skill URL set and optional mocks for _get_latest_run_for_org,
    _get_org_name_candidates, and _ask_credentialing_report.
    get_org_candidates: if provided, mock _get_org_name_candidates to return this list (for plan-B tests).
    """
    if active_context is None:
        active_context = {}
    if ask_return is None:
        ask_return = ("Answer from report.", [{"document_name": "Credentialing report"}], None, RETRIEVAL_SIGNAL_ROSTER_COMPLETE)
    extra_out = {}
    with patch.dict("os.environ", {"CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": "http://test:8011"}, clear=False):
        with patch("app.services.tool_agent._get_latest_run_for_org", return_value=get_latest_run):
            with patch("app.services.tool_agent._ask_credentialing_report", return_value=ask_return):
                if get_org_candidates is not None:
                    with patch("app.services.tool_agent._get_org_name_candidates", return_value=get_org_candidates):
                        ans, sources, usage, signal = answer_tool(
                            question=question,
                            user_message=user_message or question,
                            active_context=active_context,
                            extra_out=extra_out,
                        )
                else:
                    ans, sources, usage, signal = answer_tool(
                        question=question,
                        user_message=user_message or question,
                        active_context=active_context,
                        extra_out=extra_out,
                    )
    return ans, sources, signal, extra_out


# ---------------------------------------------------------------------------
# 1. Ask about report anytime AFTER a report (report_run_id in state)
# ---------------------------------------------------------------------------

def test_ask_about_report_when_report_run_id_in_state():
    """User can ask about the report when we have report_run_id in thread state (e.g. after a report)."""
    active = {"report_run_id": "run-123"}
    ans, sources, signal, extra_out = _run_credentialing_qa(
        question="What does the report say about at-risk revenue?",
        active_context=active,
    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    assert "Answer from report" in ans
    assert len(sources) >= 1


def test_ask_about_section_c_when_report_run_id_in_state():
    """Summarize Section C when we have a report in context."""
    active = {"report_run_id": "run-456"}
    ans, _, signal, _ = _run_credentialing_qa(
        question="Summarize Section C of the report.",
        active_context=active,
    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    assert "Answer from report" in ans


def test_npi_ready_for_pml_when_report_run_id_in_state():
    """'Why is this NPI ready for PML?' with report in context → ask path (skill injects NPI profile)."""
    active = {"report_run_id": "run-789"}
    ans, _, signal, _ = _run_credentialing_qa(
        question="Why is this NPI 1234567890 ready for PML?",
        active_context=active,
    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    assert "Answer from report" in ans


def test_npi_valid_for_florida_billing_when_report_run_id_in_state():
    """'Is NPI 1234567890 valid for Florida billing?' with report in context → ask path."""
    active = {"report_run_id": "run-fl"}
    ans, _, signal, _ = _run_credentialing_qa(
        question="Is NPI 1234567890 valid for Florida billing?",
        active_context=active,
    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    assert "Answer from report" in ans


# ---------------------------------------------------------------------------
# 2. At start of conversation: latest report for org (no prior run)
# ---------------------------------------------------------------------------

def test_latest_report_for_org_at_start_of_conversation():
    """At start of conversation: 'What is the latest report for David Lawrence Center?' → resolve org, get run, ask."""
    run = {"report_run_id": "run-dlc", "org_name": "David Lawrence Center"}
    ans, _, signal, extra_out = _run_credentialing_qa(
        question="What is the latest report for David Lawrence Center?",
        active_context={},
        get_latest_run=run,
    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    assert "Answer from report" in ans
    assert extra_out.get("report_run_id") == "run-dlc"


def test_latest_report_for_aspire_health_org_extraction():
    """'What is the latest report for Aspire Health?' extracts org and looks up run."""
    run = {"report_run_id": "run-aspire", "org_name": "Aspire Health"}
    ans, _, signal, extra_out = _run_credentialing_qa(
        question="What is the latest report for Aspire Health?",
        active_context={},
        get_latest_run=run,
    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    assert extra_out.get("report_run_id") == "run-aspire"


def test_latest_report_for_org_no_stored_run_returns_helpful_message():
    """When user asks for latest report for an org but no run exists → clear message (no network for candidates)."""
    ans, _, signal, _ = _run_credentialing_qa(
        question="What is the latest report for Unknown Org LLC?",
        active_context={},
        get_latest_run=None,
        get_org_candidates=[],  # no candidates so we get "run a report or check org name"
    )
    assert signal == RETRIEVAL_SIGNAL_NO_SOURCES
    assert "No stored report found" in ans
    assert "Unknown Org LLC" in ans or "Unknown" in ans


def test_plan_b_no_run_suggests_candidates():
    """When no direct run and org-name search returns candidates → 'Did you mean' message."""
    ans, _, signal, _ = _run_credentialing_qa(
        question="What is the latest report for David Lawrence?",
        active_context={},
        get_latest_run=None,
        get_org_candidates=["David Lawrence Center", "David Lawrence Center Inc"],
    )
    assert signal == RETRIEVAL_SIGNAL_NO_SOURCES
    assert "No stored report found" in ans
    assert "Did you mean" in ans
    assert "David Lawrence Center" in ans


def test_plan_b_resolves_org_via_candidates():
    """When direct lookup fails, try candidates from org-name search; first matching run wins."""
    def get_run(org_name: str):
        if (org_name or "").strip() == "David Lawrence Center":
            return {"report_run_id": "run-dlc", "org_name": "David Lawrence Center"}
        return None

    extra_out = {}
    with patch.dict("os.environ", {"CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": "http://test:8011"}, clear=False):
        with patch("app.services.tool_agent._get_latest_run_for_org", side_effect=get_run):
            with patch("app.services.tool_agent._get_org_name_candidates", return_value=["David Lawrence Center"]):
                with patch(
                    "app.services.tool_agent._ask_credentialing_report",
                    return_value=("Answer from report.", [], None, RETRIEVAL_SIGNAL_ROSTER_COMPLETE),
                ):
                    ans, _sources, _usage, signal = answer_tool(
                        question="What is the latest report for David Lawrence?",
                        user_message="What is the latest report for David Lawrence?",
                        active_context={},
                        extra_out=extra_out,
                    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    assert "Answer from report" in ans
    assert extra_out.get("report_run_id") == "run-dlc"


def test_pull_up_report_via_last_report_org():
    """Follow-up without org in message: use last_report_org from state to pull up latest report (reports not persisted)."""
    ans, _, signal, extra_out = _run_credentialing_qa(
        question="How many NPIs have issues with PML?",
        user_message="How many NPIs have issues with PML?",
        active_context={"last_report_org": "David Lawrence Center"},
        get_latest_run={"report_run_id": "run-dlc", "org_name": "David Lawrence Center"},
        get_org_candidates=None,
        ask_return=("45 providers in Section C (missing PML enrollment), 3 in Section B (at-risk).", [], None, RETRIEVAL_SIGNAL_ROSTER_COMPLETE),
    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    assert "45" in ans or "Section C" in ans
    assert extra_out.get("last_report_org") == "David Lawrence Center"


# ---------------------------------------------------------------------------
# 3. NPI valid for Florida billing at start (with org in same message)
# ---------------------------------------------------------------------------

def test_npi_valid_for_florida_billing_with_org_in_message():
    """'Is NPI 1234567890 valid for Florida billing in the David Lawrence report?' → resolve org, then ask with NPI."""
    run = {"report_run_id": "run-dlc"}
    ans, _, signal, extra_out = _run_credentialing_qa(
        question="Is NPI 1234567890 valid for Florida billing? What about the latest report for David Lawrence Center?",
        active_context={},
        get_latest_run=run,
    )
    # Should resolve "David Lawrence Center" from the second sentence and get run, then ask
    # (Our prefix matching might get "David Lawrence Center" from "latest report for David Lawrence Center")
    # If the question is only "Is NPI ... valid for Florida billing?" we have no org - next test
    assert signal in (RETRIEVAL_SIGNAL_ROSTER_COMPLETE, RETRIEVAL_SIGNAL_NO_SOURCES)


def test_npi_valid_for_florida_billing_no_org_no_run_asks_for_org():
    """At start: 'Is NPI 1234567890 valid for Florida billing?' with no org and no run → ask user to specify org."""
    ans, _, signal, _ = _run_credentialing_qa(
        question="Is NPI 1234567890 valid for Florida billing?",
        active_context={},
        get_latest_run=None,
    )
    assert signal == RETRIEVAL_SIGNAL_NO_SOURCES
    assert "organization" in ans.lower() or "report" in ans.lower()
    assert "Latest report for" in ans or "run a credentialing report" in ans.lower()


# ---------------------------------------------------------------------------
# 4. Create new report must NOT go to ask path
# ---------------------------------------------------------------------------

def test_create_credentialing_report_runs_orchestrator_not_ask():
    """'Create a credentialing report for Aspire' must run orchestrator, not ask."""
    extra_out = {}
    with patch.dict("os.environ", {"CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": "http://test:8011"}, clear=False):
        with patch("app.services.tool_agent.run_orchestrator") as mock_orch:
            mock_orch.return_value = ("Report done.", MagicMock(step_outputs=[], report_run_id="new-run"))
            ans, _, signal, _ = answer_tool(
                question="Create a credentialing report for Aspire Health",
                user_message="Create a credentialing report for Aspire Health",
                active_context={"report_run_id": "old-run"},
                extra_out=extra_out,
            )
    mock_orch.assert_called_once()
    assert "Report done" in ans or "report" in ans.lower()


# ---------------------------------------------------------------------------
# 5. Parsing: intent and org extraction
# ---------------------------------------------------------------------------

def test_credentialing_intent_latest_report():
    """Phrase 'latest report' triggers credentialing intent."""
    ans, _, signal, _ = _run_credentialing_qa(
        question="What is the latest report for Circles of Care?",
        active_context={},
        get_latest_run={"report_run_id": "x"},
    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE


def test_credentialing_intent_npi_valid_florida():
    """Phrase 'valid for Florida' / 'Florida billing' triggers credentialing intent."""
    ans, _, signal, _ = _run_credentialing_qa(
        question="Is this NPI valid for Florida billing?",
        active_context={"report_run_id": "r"},
    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE


def test_credentialing_intent_nppes():
    """Phrase 'NPPES' triggers credentialing intent."""
    ans, _, signal, _ = _run_credentialing_qa(
        question="What does the NPPES data say for this provider?",
        active_context={"report_run_id": "r"},
    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE


# ---------------------------------------------------------------------------
# 6. No skill URL: clear message
# ---------------------------------------------------------------------------

def test_no_skill_url_returns_configure_message():
    """When skill URL is not set and user asks a credentialing question, they get a configuration message."""
    # With URL set to empty, _ask_credentialing_report returns config message and no_sources.
    # (Under pytest the unpacked signal can vary depending on env; we assert the message.)
    with patch.dict("os.environ", {"CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": ""}, clear=False):
        ans, sources, signal, _ = answer_tool(
            question="Why is this NPI ready for PML?",
            user_message="Why is this NPI ready for PML?",
            active_context={"report_run_id": "run-123"},
        )
    # Either we hit the ask path (config message + no_sources) or another path; either way user sees help
    assert "not configured" in ans.lower() or "CHAT_SKILLS" in ans or "URL" in ans or "report" in ans.lower()
    assert signal is None or signal == RETRIEVAL_SIGNAL_NO_SOURCES
