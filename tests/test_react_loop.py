"""Tests for ReAct loop: Reason → Act → Observe."""
from unittest.mock import patch, MagicMock

import pytest

from app.pipeline.context import PipelineContext
# 2026-04-18 disconnect cleanup: _envelope_routes_to_reconciliation
# was removed along with the credentialing glue. This test file
# predates the credentialing disconnect; it still exercises the core
# ReAct loop behavior, just without the credentialing helper check.
from app.pipeline.react_loop import (
    REACT_MAX_ROUNDS_AGENTIC,
    REACT_MAX_ROUNDS_COPILOT,
    build_reasoning_context,
    _execute_tool,
    _finalize_response,
    _make_react_plan,
    _parse_react_decision_json,
    _react_fallback_org_npi_lookup_decision,
    _react_reasoning_system,
    react_chat_mode_label,
    react_max_iterations_for_mode,
    run_react,
)


def test_react_max_iterations_respects_chat_mode():
    assert react_max_iterations_for_mode("agentic") == REACT_MAX_ROUNDS_AGENTIC
    assert react_max_iterations_for_mode("  AGENTIC ") == REACT_MAX_ROUNDS_AGENTIC
    assert react_max_iterations_for_mode("copilot") == REACT_MAX_ROUNDS_COPILOT
    assert react_max_iterations_for_mode(None) == REACT_MAX_ROUNDS_COPILOT


def test_react_chat_mode_label():
    assert react_chat_mode_label("agentic") == "agentic"
    assert react_chat_mode_label("  AGENTIC ") == "agentic"
    assert react_chat_mode_label("copilot") == "copilot"
    assert react_chat_mode_label(None) == "copilot"


def test_react_reasoning_system_includes_mode_quality_bar():
    cop = _react_reasoning_system(3, "copilot")
    assert "**copilot**" in cop
    assert "reasonable" in cop.lower()
    ag = _react_reasoning_system(6, "agentic")
    assert "**agentic**" in ag
    assert "precision" in ag.lower()


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

    # Disable the critic gate for this test (2026-04-23). The test asserts
    # the raw reason→tool→reason→complete sequence — not critic integration.
    # With MOBIUS_REACT_CRITIC=1 (the default in dev.env) an extra critic
    # audit LLM call fires after is_complete=true, exhausting
    # fake_llm with StopIteration. Critic integration is covered by
    # test_react_critic.py.
    with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
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

    # Disable the critic gate (2026-04-23). Same reason as
    # test_run_react_one_iteration_then_complete: this test asserts the
    # exact call count (2) for the reasoning LLM, and the default-enabled
    # critic would add a third call + exhaust the side_effect iterator.
    with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
         patch("app.pipeline.react_loop._call_llm_json") as mock_llm:
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
