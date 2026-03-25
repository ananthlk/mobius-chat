"""Tests for ReAct loop: Reason → Act → Observe."""
from unittest.mock import patch, MagicMock

import pytest

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import (
    build_reasoning_context,
    _execute_tool,
    _envelope_routes_to_reconciliation,
    _finalize_response,
    _make_react_plan,
    _parse_react_decision_json,
    _react_fallback_org_npi_lookup_decision,
    run_react,
    MAX_ITERATIONS,
)


def test_build_reasoning_context_includes_jurisdiction_and_message():
    """build_reasoning_context includes active jurisdiction and user message."""
    ctx = PipelineContext(
        correlation_id="test",
        thread_id="t1",
        message="What is Sunshine Health's PA process?",
    )
    ctx.merged_state = {"active": {"payer": "Sunshine Health", "jurisdiction": "Florida"}}
    ctx.effective_message = ctx.message
    ctx.last_turns = []
    out = build_reasoning_context(ctx, [], 1)
    assert "Sunshine" in out or "Florida" in out
    assert "What is Sunshine" in out


def test_build_reasoning_context_includes_tool_results_after_act():
    """After one tool call, next reasoning context includes tool result preview."""
    ctx = PipelineContext(correlation_id="c", thread_id=None, message="PA process?")
    ctx.effective_message = ctx.message
    tool_results = [
        {"tool": "search_corpus", "success": False, "result": "No relevant documents found."},
    ]
    out = build_reasoning_context(ctx, tool_results, 2)
    assert "search_corpus" in out
    assert "No relevant" in out or "Iteration" in out


def test_execute_tool_refuse_returns_terminal():
    """refuse tool returns is_terminal=True and does not run RAG/tools."""
    ctx = PipelineContext(correlation_id="c", thread_id=None, message="Is member 12345 eligible?")
    ctx.effective_message = ctx.message
    r = _execute_tool("refuse", {"reason": "PHI"}, ctx, None)
    assert r["tool"] == "refuse"
    assert r.get("is_terminal") is True
    assert r.get("success") is False


def test_finalize_response_sets_plan_answers_and_answer_set():
    """_finalize_response sets ctx.plan, ctx.answers, ctx.answer_set for integrate."""
    ctx = PipelineContext(correlation_id="c", thread_id=None, message="What is PA?")
    ctx.effective_message = ctx.message
    _finalize_response(
        ctx,
        final_answer="PA is required for H0036.",
        all_sources=[{"document_name": "Manual", "index": 1}],
        final_signal="corpus_only",
        last_tool="search_corpus",
        emitter=None,
    )
    assert ctx.plan is not None
    assert len(ctx.plan.subquestions) == 1
    assert ctx.plan.subquestions[0].id == "react_main"
    assert ctx.answers == ["PA is required for H0036."]
    assert "react_main" in ctx.answer_set
    assert ctx.answer_set["react_main"]["answer"] == "PA is required for H0036."
    assert ctx.sources
    assert ctx.retrieval_signals == ["corpus_only"]


def test_run_react_one_iteration_then_complete():
    """
    ReAct loop: first LLM call returns search_corpus, we execute it;
    second LLM call returns is_complete=true with answer; we finalize.
    """
    ctx = PipelineContext(
        correlation_id="react-test",
        thread_id=None,
        message="What is Sunshine Health's PA requirement for H0036?",
    )
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.effective_message = ctx.message

    reason_count = 0

    def fake_llm(system: str, user: str, max_tokens: int = 800, ctx=None, stage: str = "planner", **kwargs) -> str:
        nonlocal reason_count
        reason_count += 1
        if reason_count == 1:
            return '{"thought": "Try corpus first.", "tool": "search_corpus", "inputs": {"query": "Sunshine Health PA H0036"}, "is_complete": false}'
        if reason_count == 2:
            return (
                '{"thought": "Corpus had the answer.", "tool": null, "inputs": {}, "is_complete": true, '
                '"answer": "Prior authorization is required for H0036.", "sources": [], "confidence": "high"}'
            )
        return '{"tool": null, "is_complete": true, "answer": "I could not find an answer."}'

    with patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
        with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
            mock_execute.return_value = {
                "tool": "search_corpus",
                "success": True,
                "result": "Prior authorization is required for H0036 for Sunshine Health.",
                "signal": "corpus_only",
                "sources": [{"document_name": "Provider Manual", "index": 1}],
                "usage": None,
            }
            run_react(ctx, emitter=None)

    assert reason_count == 2, "Expected 2 reasoner calls: tool choice, then complete"
    assert mock_execute.call_count == 1
    assert mock_execute.call_args[0][0] == "search_corpus"
    assert ctx.final_message == "Prior authorization is required for H0036."
    assert ctx.plan is not None
    assert "react_main" in ctx.answer_set
    assert ctx.retrieval_signals == ["corpus_only"]


def test_run_react_follow_up_from_active_context_skips_tools():
    """When message is a follow-up to active_context, we answer from context and do not call LLM for tool choice."""
    active_context = {
        "tool": "run_credentialing_report",
        "org": "David Lawrence Center",
        "summary": "Section B: 3 providers. Section C: 45 providers. Total PML issues: 48.",
        "full_output": "Readiness 46%. Section B: 3. Section C: 45. Total opportunity $1.9M.",
        "follow_up_capable": True,
        "expires_after_turns": 5,
    }
    ctx = PipelineContext(
        correlation_id="react-followup",
        thread_id="t1",
        message="How many NPIs have issues with PML?",
    )
    ctx.merged_state = {"active_context": active_context}
    ctx.last_turns = []
    ctx.effective_message = ctx.message

    with patch("app.pipeline.react_loop._call_llm_json") as mock_llm:
        with patch("app.pipeline.react_loop.answer_reasoning") as mock_reasoning:
            mock_reasoning.return_value = (
                "48 NPIs have PML issues: 3 at-risk in Section B and 45 missing enrollment in Section C.",
                None,
            )
            run_react(ctx, emitter=None)

    mock_llm.assert_not_called()
    mock_reasoning.assert_called_once()
    assert ctx.final_message
    assert "48" in ctx.final_message or "Section" in ctx.final_message
    assert ctx.active_skill_reference is True


def test_complete_answer_finalizes():
    """When reasoning returns is_complete=true with answer, we finalize and return."""
    ctx = PipelineContext(correlation_id="c", thread_id=None, message="What is PA for H0036?")
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.effective_message = ctx.message

    with patch("app.pipeline.react_loop._call_llm_json") as mock_llm:
        mock_llm.side_effect = [
            '{"tool": "search_corpus", "inputs": {}, "is_complete": false}',
            '{"tool": null, "is_complete": true, "answer": "PA is required for H0036.", "sources": [], "confidence": "high"}',
        ]
        with patch("app.pipeline.react_loop._execute_tool") as mock_exec:
            mock_exec.return_value = {
                "tool": "search_corpus",
                "success": True,
                "result": "PA required.",
                "signal": "corpus_only",
                "sources": [],
                "usage": None,
            }
            run_react(ctx, emitter=None)

    assert ctx.final_message == "PA is required for H0036."
    assert mock_llm.call_count == 2  # reason, reason (with answer)


def test_execute_tool_ask_credentialing_npi_no_report_returns_failure():
    """ask_credentialing_npi with no report in context returns success=False and suggests healthcare_npi_lookup."""
    ctx = PipelineContext(
        correlation_id="c",
        thread_id=None,
        message="Is NPI 1927298609 set up for PML?",
    )
    ctx.merged_state = {"active": {}}
    ctx.effective_message = ctx.message

    with patch("app.pipeline.react_loop.answer_tool") as mock_tool:
        mock_tool.return_value = (
            "I don't have a report in this thread. Run a credentialing report first.",
            [],
            None,
            "no_sources",
        )
        r = _execute_tool(
            "ask_credentialing_npi",
            {"question": "Is NPI 1927298609 set up for PML?"},
            ctx,
            None,
        )

    assert r["tool"] == "ask_credentialing_npi"
    assert r["success"] is False
    assert "healthcare_npi_lookup" in r["result"] or "NPPES" in r["result"]
    mock_tool.assert_called_once()
    call_kw = mock_tool.call_args[1]
    assert call_kw.get("tool_hint_override") == "credentialing_qa"


def test_execute_tool_lookup_npi_passes_pipeline_ctx():
    """lookup_npi forwards PipelineContext so NPI disambiguation can attach clarification_options."""
    ctx = PipelineContext(correlation_id="c", thread_id=None, message="NPI for Test Org Inc")
    ctx.merged_state = {"active": {}}
    ctx.effective_message = ctx.message

    with patch("app.pipeline.react_loop.answer_tool") as mock_tool:
        mock_tool.return_value = ("## NPI lookup\n`123`", [], None, "no_sources")
        _execute_tool(
            "lookup_npi",
            {"org_name": "Test Org Inc"},
            ctx,
            None,
        )
    mock_tool.assert_called_once()
    assert mock_tool.call_args[1].get("pipeline_ctx") is ctx


def test_execute_tool_find_org_locations_passes_hint_and_inputs():
    """find_org_locations calls answer_tool with find_org_locations hint and merged tool_inputs."""
    ctx = PipelineContext(
        correlation_id="c",
        thread_id=None,
        message="Find practice locations for 1234567893",
    )
    ctx.merged_state = {"active": {}}
    ctx.effective_message = ctx.message
    ctx.chat_mode = "copilot"

    with patch("app.pipeline.react_loop.answer_tool") as mock_tool:
        mock_tool.return_value = (
            "# Practice locations (1 site(s))\n\n## Sites\n\n1. **1 Main**, Tampa, FL 33601",
            [],
            None,
            "no_sources",
        )
        r = _execute_tool(
            "find_org_locations",
            {"org_npis": ["1234567893"]},
            ctx,
            None,
        )

    assert r["tool"] == "find_org_locations"
    assert r["success"] is True
    mock_tool.assert_called_once()
    call_kw = mock_tool.call_args[1]
    assert call_kw.get("tool_hint_override") == "find_org_locations"
    assert call_kw.get("tool_inputs") == {"org_npis": ["1234567893"]}


def test_execute_tool_find_associated_providers_at_locations_passes_hint_and_inputs():
    """find_associated_providers_at_locations calls answer_tool with matching hint and merged tool_inputs."""
    ctx = PipelineContext(
        correlation_id="c",
        thread_id=None,
        message="Who practices at each site for org NPI 1234567893",
    )
    ctx.merged_state = {"active": {}}
    ctx.effective_message = ctx.message
    ctx.chat_mode = "copilot"

    with patch("app.pipeline.react_loop.answer_tool") as mock_tool:
        mock_tool.return_value = (
            "# Providers implicated at each practice site\n\n## 1 Main St\n\n1. **NPI 1111111111**",
            [],
            None,
            "no_sources",
        )
        r = _execute_tool(
            "find_associated_providers_at_locations",
            {"org_npis": ["1234567893"]},
            ctx,
            None,
        )

    assert r["tool"] == "find_associated_providers_at_locations"
    assert r["success"] is True
    mock_tool.assert_called_once()
    call_kw = mock_tool.call_args[1]
    assert call_kw.get("tool_hint_override") == "find_associated_providers_at_locations"
    assert call_kw.get("tool_inputs") == {"org_npis": ["1234567893"]}


def test_execute_tool_healthcare_npi_lookup_calls_answer_tool():
    """healthcare_npi_lookup calls answer_tool with healthcare_query hint."""
    ctx = PipelineContext(
        correlation_id="c",
        thread_id=None,
        message="Look up NPI 1927298609",
    )
    ctx.merged_state = {"active": {}}
    ctx.effective_message = ctx.message

    with patch("app.pipeline.react_loop.answer_tool") as mock_tool:
        mock_tool.return_value = (
            "NPI 1927298609: John Doe, Taxonomy 101Y00000X Counselor, Address 123 Main St.",
            [{"document_name": "NPPES", "index": 1}],
            None,
            "no_sources",
        )
        r = _execute_tool(
            "healthcare_npi_lookup",
            {"question": "Look up NPI 1927298609"},
            ctx,
            None,
        )

    assert r["tool"] == "healthcare_npi_lookup"
    assert r["success"] is True
    mock_tool.assert_called_once()
    call_kw = mock_tool.call_args[1]
    assert call_kw.get("tool_hint_override") == "healthcare_query"


def test_parse_react_decision_json_plain():
    raw = '{"thought": "x", "tool": "search_corpus", "inputs": {"query": "a"}, "is_complete": false}'
    d = _parse_react_decision_json(raw)
    assert d is not None
    assert d.get("tool") == "search_corpus"


def test_parse_react_decision_json_markdown_fence():
    raw = '```json\n{"is_complete": true, "answer": "Done"}\n```'
    d = _parse_react_decision_json(raw)
    assert d is not None
    assert d.get("is_complete") is True


def test_parse_react_decision_json_prefixed_and_braces_in_answer():
    """Prose before JSON + `}` inside a string value must not confuse extraction."""
    import json as _json

    obj = {"thought": "Grounding", "is_complete": True, "answer": "Line with } and { in markdown."}
    raw = "Here is my analysis:\n\n" + _json.dumps(obj)
    d = _parse_react_decision_json(raw)
    assert d is not None
    assert d.get("is_complete") is True
    assert "}" in (d.get("answer") or "")


def test_react_fallback_org_npi_lookup_extracts_name():
    ctx = PipelineContext(correlation_id="c", thread_id="t", message="")
    ctx.effective_message = (
        "okay find the NPIs for Aspire Health and I can help you select the right ones"
    )
    d = _react_fallback_org_npi_lookup_decision(ctx)
    assert d is not None
    assert d.get("tool") == "lookup_npi"
    assert d.get("inputs", {}).get("org_name") == "Aspire Health"


def test_parse_react_decision_json_trailing_comma_repaired():
    """LLMs often emit trailing commas; json_repair should recover."""
    raw = '{"thought": "ok", "tool": null, "inputs": {}, "is_complete": true, "answer": "Hi",}'
    d = _parse_react_decision_json(raw)
    assert d is not None
    assert d.get("is_complete") is True
    assert d.get("answer") == "Hi"


def test_envelope_routes_to_reconciliation_roster_on_thread():
    ctx = PipelineContext(correlation_id="c", thread_id="t", message="credentialing report for Org")
    ctx.credentialing_options = {"org_name": "Org", "mode": "autopilot"}
    ctx.merged_state = {
        "active": {
            "reconciliation_upload_id": "u1",
            "reconciliation_org_id": "1234567890",
            "reconciliation_org_name": "Org",
        }
    }
    assert _envelope_routes_to_reconciliation(ctx) is True


def test_envelope_routes_to_reconciliation_prefer_outside_in():
    ctx = PipelineContext(correlation_id="c", thread_id="t", message="run report")
    ctx.credentialing_options = {
        "org_name": "Org",
        "prefer_outside_in": True,
    }
    ctx.merged_state = {
        "active": {"reconciliation_upload_id": "u1", "reconciliation_org_id": "1234567890"}
    }
    assert _envelope_routes_to_reconciliation(ctx) is False


def test_envelope_routes_to_reconciliation_message_outside_in():
    ctx = PipelineContext(correlation_id="c", thread_id="t", message="outside-in medicaid npi report for Org")
    ctx.credentialing_options = {"org_name": "Org"}
    ctx.merged_state = {
        "active": {"reconciliation_upload_id": "u1", "reconciliation_org_id": "1234567890"}
    }
    assert _envelope_routes_to_reconciliation(ctx) is False


def test_envelope_routes_to_reconciliation_no_envelope_ignored():
    ctx = PipelineContext(correlation_id="c", thread_id="t", message="hi")
    ctx.merged_state = {
        "active": {"reconciliation_upload_id": "u1", "reconciliation_org_id": "1234567890"}
    }
    assert _envelope_routes_to_reconciliation(ctx) is False


def test_detect_skill_reference_lookup_npi_practice_locations_follow_up():
    """active_context.tool is lookup_npi — must match SKILL_TERMS['lookup_npi'] for location follow-ups."""
    from app.pipeline.message_resolver import detect_skill_reference

    skill_like = {"skill": "lookup_npi", "org": "Aspire Health", "summary": "candidates…"}
    ok, name = detect_skill_reference(
        "Can you find the practice locations tied to these NPIs?",
        skill_like,
    )
    assert ok is True
    assert name == "lookup_npi"


def test_detect_skill_reference_not_when_chip_payload_and_locations():
    """Workflow chip text includes billing NPI lines — must run tools, not _answer_from_context."""
    from app.pipeline.message_resolver import detect_skill_reference

    skill_like = {"skill": "lookup_npi", "org": "Aspire Health", "summary": "candidates…"}
    msg = (
        "[Mobius workflow_selection]\n"
        "• Use billing NPI 1366813586 for ASPIRE HEALTH PARTNERS\n"
        "find the locations"
    )
    ok, name = detect_skill_reference(msg, skill_like)
    assert ok is False
    assert name is None


def test_detect_skill_reference_not_when_10digit_npi_and_find_locations():
    from app.pipeline.message_resolver import detect_skill_reference

    skill_like = {"skill": "lookup_npi", "org": "Aspire Health", "summary": "candidates…"}
    ok, name = detect_skill_reference(
        "Find practice locations for NPI 1366813586",
        skill_like,
    )
    assert ok is False
    assert name is None
