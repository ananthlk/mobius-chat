"""react_trace diagnostics telemetry (2026-07-31).

Ananth's ask: the emit trail was "blah" — static positional headlines
("Round 2/3 — Grounding — use evidence from prior tool results") that
don't reflect what's actually happening that round, and nothing from the
Product Promise governor (directive/reason), the mandatory groundedness
floor, or the final-round self-report ever reached the user or a
diagnostics surface — only server logs.

Two things changed in react_loop.py:
  1. The round headline now uses the governor's real directive+reason
     when it's active, instead of the static per-position label.
  2. A new ``react_trace`` envelope (make_react_trace, emit_envelope.py)
     is built once per turn in _finalize_response and appended to
     ctx.thinking_chunks — same "diagnostic-only" tier and Diagnostics-tab
     pattern already established by retrieval_trace, just for the react
     loop's own execution instead of a single corpus search.

This file locks: the envelope's presence/shape, that it degrades
gracefully with the governor off, and that headlines actually change.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import run_react

_SEARCH_RESULT = {
    "tool": "search_corpus",
    "success": True,
    "result": "a reasonably long snippet of retrieved policy text",
    "signal": "corpus_only",
    "sources": [],
    "usage": None,
}


def _make_ctx(chat_mode: str) -> PipelineContext:
    ctx = PipelineContext(correlation_id="trace-test", thread_id=None, message="What is the policy?")
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = chat_mode
    ctx.thinking_chunks = []
    return ctx


def _trace_entries(ctx) -> list[dict]:
    return [c for c in ctx.thinking_chunks if isinstance(c, dict) and c.get("signal") == "react_trace"]


def test_trace_emitted_exactly_once_with_governor_off():
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage == "react_1":
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real answer here", "confidence": "high"}'

    ctx = _make_ctx("copilot")
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "", "MOBIUS_REACT_CRITIC": ""}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    traces = _trace_entries(ctx)
    assert len(traces) == 1
    data = traces[0]["data"]
    assert data["mode"] == "copilot"
    assert data["rounds_used"] == 2
    assert data["governor_enabled"] is False
    assert len(data["rounds"]) == 2
    # Governor off -> no directive/reason per round, but the round entries
    # still exist (round number always populates).
    assert data["rounds"][0]["directive"] is None
    assert data["rounds"][0]["round"] == 1
    assert data["groundedness_floor_ran"] is False
    assert data["unfinished_reason"] is None


def test_trace_captures_governor_directive_and_completion():
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage == "react_1":
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        if stage == "critique":
            return '{"grounded": true, "issues": []}'
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real, sufficiently long, grounded answer here", "confidence": "high"}'

    ctx = _make_ctx("copilot")
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "1"}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    traces = _trace_entries(ctx)
    assert len(traces) == 1
    data = traces[0]["data"]
    assert data["governor_enabled"] is True
    assert data["rounds"][0]["directive"] == "search"
    assert isinstance(data["rounds"][0]["reason"], str) and data["rounds"][0]["reason"]
    # Model-bandit selection criteria (2026-08-04) — must be visible in the
    # same diagnostics panel, not just driving the call invisibly. Caught
    # as a real gap (built the criteria, never added them here) when
    # Ananth asked directly whether this was actually in the diagnostics.
    assert data["rounds"][0]["reasoning_depth"] == "fast"  # directive=search -> explore -> fast
    assert "latency_budget_ms" in data["rounds"][0]
    assert data["groundedness_floor_ran"] is True
    assert data["groundedness_passed"] is True
    assert data["final_directive"] == "complete"
    # 2026-08-04 (Chat Architecture, reviewing for Bandit Agent's reward
    # signal): hard_ceiling_s was ALWAYS None in every real trace -- the
    # field existed on make_react_trace()'s signature but nothing ever
    # actually passed a value for it. Locks in the fix.
    assert data["hard_ceiling_s"] is not None
    assert data["hard_ceiling_s"] > 0


def test_trace_groundedness_score_none_when_heuristic_flag_off():
    """2026-08-04 (Bandit Agent reward contract, Eval/Chat Architecture
    design review): the continuous groundedness heuristic is scaffolded
    behind its own flag (MOBIUS_REACT_GROUNDEDNESS_HEURISTIC), separate
    from MOBIUS_PRODUCT_PROMISE_ENABLED -- must stay None in the trace
    even when the mandatory groundedness floor runs and passes."""
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage == "react_1":
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        if stage == "critique":
            return '{"grounded": true, "issues": []}'
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real, sufficiently long, grounded answer here", "confidence": "high"}'

    ctx = _make_ctx("copilot")
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "1", "MOBIUS_REACT_GROUNDEDNESS_HEURISTIC": ""}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    data = _trace_entries(ctx)[0]["data"]
    assert data["groundedness_score"] is None


def test_trace_groundedness_score_populated_when_heuristic_flag_on():
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage == "react_1":
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        if stage == "critique":
            return '{"grounded": false, "issues": [{"claim": "x", "severity": "medium", "reason": "r"}]}'
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real, sufficiently long, grounded answer here", "confidence": "high"}'

    ctx = _make_ctx("copilot")
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "1", "MOBIUS_REACT_GROUNDEDNESS_HEURISTIC": "1"}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    data = _trace_entries(ctx)[0]["data"]
    # medium severity, default weight 0.2 -> 1 - 0.2 = 0.8
    assert data["groundedness_score"] == pytest.approx(0.8)


def test_trace_hard_ceiling_s_is_none_when_governor_off():
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real answer here", "confidence": "high"}'

    ctx = _make_ctx("copilot")
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": ""}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    data = _trace_entries(ctx)[0]["data"]
    assert data["hard_ceiling_s"] is None, "no contract exists without the governor -- no ceiling to report"


def test_trace_captures_unfinished_self_report():
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage in ("react_1", "react_2"):
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return (
            '{"thought": "stuck", "tool": null, "inputs": {}, "is_complete": false, '
            '"unfinished_reason": "no_path_forward", '
            '"unfinished_summary": "Checked corpus and web, nothing on this payer."}'
        )

    ctx = _make_ctx("copilot")
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "1"}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    traces = _trace_entries(ctx)
    assert len(traces) == 1
    data = traces[0]["data"]
    assert data["unfinished_reason"] == "no_path_forward"
    assert "nothing on this payer" in data["unfinished_summary"]
    assert data["final_directive"] is None, "unfinished path never reaches the completion marker"


def test_headline_uses_governor_directive_when_active():
    """The emitted 'Round N/M — ...' line should carry the governor's
    real reason text, not the static positional label, when the
    governor is on."""
    seen_headlines = []

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage == "react_1":
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real answer here", "confidence": "high"}'

    def collector(m):
        if isinstance(m, str) and m.strip().startswith("Round"):
            seen_headlines.append(m)

    ctx = _make_ctx("copilot")
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "1"}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=collector)

    assert seen_headlines, "expected at least one round headline emit"
    # The governor's real reason text ("confidence bar not met...") should
    # appear verbatim instead of the static "Scoping"/"Grounding" labels.
    assert any("confidence bar" in h for h in seen_headlines)
    assert not any("Scoping — interpret" in h for h in seen_headlines)


def test_headline_falls_back_to_static_label_when_governor_off():
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real answer here", "confidence": "high"}'

    seen_headlines = []

    def collector(m):
        if isinstance(m, str) and m.strip().startswith("Round"):
            seen_headlines.append(m)

    ctx = _make_ctx("copilot")
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": ""}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=collector)

    assert seen_headlines
    assert any("Scoping — interpret" in h for h in seen_headlines)


def test_rag_call_rounds_threaded_into_the_trace_envelope():
    """2026-08-09, Chat Master directive, add-on to #80: the trace
    envelope's data must carry whatever ctx._rag_call_rounds holds --
    _execute_tool_with_retry is mocked in this file's harness (so the
    real accumulation logic, covered in test_react_low_confidence_
    escalation.py, never runs here), so pre-setting ctx._rag_call_rounds
    directly isolates the WIRING (make_react_trace's call site reads and
    threads it through) from the accumulation logic itself."""
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage == "react_1":
            return '{"thought": "search", "tool": "search_corpus", "inputs": {"query": "x"}, "is_complete": false}'
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real answer here", "confidence": "high"}'

    ctx = _make_ctx("copilot")
    ctx._rag_call_rounds = [
        {"round_n": 1, "query": "x", "terminal_action": "clarify_low_confidence", "module_trace": [{"stage": "route"}], "latency_ms": {"total_ms": 300}},
        {"round_n": 2, "query": "x", "terminal_action": None, "module_trace": [{"stage": "route"}], "latency_ms": {"total_ms": 450}},
    ]
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": "", "MOBIUS_REACT_CRITIC": ""}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    data = _trace_entries(ctx)[0]["data"]
    assert len(data["rag_call_rounds"]) == 2
    assert data["rag_call_rounds"][0]["round_n"] == 1
    assert data["rag_call_rounds"][1]["terminal_action"] is None


def test_rag_call_rounds_defaults_to_empty_list_when_absent():
    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        return '{"thought": "done", "tool": null, "inputs": {}, "is_complete": true, "answer": "a real answer here", "confidence": "high"}'

    ctx = _make_ctx("copilot")
    with patch.dict("os.environ", {"MOBIUS_PRODUCT_PROMISE_ENABLED": ""}), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
         patch("app.pipeline.react_loop._execute_tool_with_retry", return_value=_SEARCH_RESULT):
        run_react(ctx, emitter=None)

    data = _trace_entries(ctx)[0]["data"]
    assert data["rag_call_rounds"] == []
