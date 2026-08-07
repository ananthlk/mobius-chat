"""Tool results must never be truncated in react's reasoning context
(2026-08-07, Ananth, live-query finding).

build_reasoning_context() used to cap every tool result string to a
320-head + 400-tail slice once it exceeded 600 chars. rag/corpus_search
never sets result_summary, so every rag result -- however many chunks it
returned -- fell into that slice. Confirmed live: a 7.8k-char, 7-chunk
rag result had its one chunk containing the literal answer (ranked #4,
authority=authoritative) sliced out of the middle and silently dropped,
so react concluded "cannot answer" over evidence it never actually saw.

No tool gets a length cap now. If a result is too expensive to show in
full, that's a per-tool decision via result_summary -- and even then the
full result is now shown alongside the summary, not instead of it.
"""

from __future__ import annotations

from app.pipeline.context import PipelineContext
from app.pipeline.react.prompts import build_reasoning_context


def _make_ctx() -> PipelineContext:
    ctx = PipelineContext(correlation_id="no-trunc-test", thread_id=None, message="does it matter")
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = "agentic"
    ctx.thinking_chunks = []
    return ctx


def test_long_result_without_summary_is_not_truncated():
    """The exact failure mode: a >600-char rag result with no
    result_summary used to become raw[:320] + "..." + raw[-400:]."""
    middle_marker = "THE-ANSWER-IS-HERE-180-CALENDAR-DAYS"
    raw = ("A" * 400) + middle_marker + ("B" * 400)
    tool_results = [{"tool": "rag", "result": raw, "success": True}]
    out = build_reasoning_context(_make_ctx(), tool_results, 2)
    assert middle_marker in out
    assert "[truncated]" not in out
    assert raw in out


def test_full_result_length_stated_and_accurate():
    raw = "X" * 5000
    tool_results = [{"tool": "rag", "result": raw, "success": True}]
    out = build_reasoning_context(_make_ctx(), tool_results, 1)
    assert "5000 chars, complete, not truncated" in out


def test_summary_present_shows_summary_and_full_result_both():
    """result_summary (used by NPPES/healthcare tools) is a helpful
    anchor, not a replacement -- the full raw result must still appear so
    react can ground on the actual evidence, not just a précis."""
    middle_marker = "OBSCURE-DETAIL-ONLY-IN-THE-RAW-BODY"
    raw = ("A" * 400) + middle_marker + ("B" * 400)
    tool_results = [{
        "tool": "healthcare_npi_lookup",
        "result": raw,
        "result_summary": "NPI 1234567890 found, active, FL",
        "success": True,
    }]
    out = build_reasoning_context(_make_ctx(), tool_results, 2)
    assert "NPI 1234567890 found, active, FL" in out
    assert middle_marker in out


def test_multiple_prior_rounds_each_kept_in_full():
    """Every prior round's rag call stays fully visible -- not just the
    most recent one, and not each independently re-clipped."""
    marker_1 = "ROUND-ONE-FACT-XYZ"
    marker_2 = "ROUND-TWO-FACT-ABC"
    raw_1 = ("P" * 400) + marker_1 + ("Q" * 400)
    raw_2 = ("P" * 400) + marker_2 + ("Q" * 400)
    tool_results = [
        {"tool": "rag", "result": raw_1, "success": True},
        {"tool": "rag", "result": raw_2, "success": True},
    ]
    out = build_reasoning_context(_make_ctx(), tool_results, 3)
    assert marker_1 in out
    assert marker_2 in out


def test_short_result_unaffected():
    raw = "short and simple result"
    tool_results = [{"tool": "rag", "result": raw, "success": True}]
    out = build_reasoning_context(_make_ctx(), tool_results, 1)
    assert raw in out
