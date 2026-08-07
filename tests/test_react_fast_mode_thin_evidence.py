"""Fast-mode thin-evidence handling (2026-08-07, Task #65 follow-up +
"always summarize" grace rule, Chat Master relaying Ananth's UX
contract).

The round-1 early exit in fast/"quick" mode used to ship whatever the
first rag call returned verbatim -- confirmed live (cid d288d009) as
the same fabrication mechanism #65 fixed for the normal reasoning path.

Ananth's principles, current shape:
1. Always stream something fast -- the thin-evidence path adds ONE
   lightweight synthesis call (not a full reasoning round: no tool-call
   decision schema, no evidence_review) -- materially faster than
   agentic's multi-round path on the same evidence, even though it's no
   longer literally zero-LLM-calls. ("one pass instead of two rounds")
2. Never fabricate -- the synthesis call is citation-disciplined
   (state only what's literally in the evidence, say what's missing
   rather than guess) -- same spirit as rule 1c-2's citation-discipline
   rule from #65. Falls back to the pure code-constructed excerpt
   (_build_fast_mode_hedge) if the synthesis call itself fails.
3. "Always let ReAct summarize, even on early exit" (2026-08-07,
   Ananth, directly, a grace rule): thin evidence still gets a real
   best-effort answer, not just "I couldn't find enough" -- the hedge
   and suggest_escalate accompany the summary, they don't replace it.
"""
from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import (
    _FAST_MODE_MIN_CHARS,
    _FAST_MODE_MIN_CHUNKS,
    _all_chunk_stats,
    _build_fast_mode_hedge,
    run_react,
)

_RICH_CHUNKS = "\n\n".join(
    f"[{i}] Doc {i}\n" + ("Detailed relevant content about care management programs. " * 6)
    for i in range(1, 4)
)
_THIN_CHUNKS = "[1] Doc A\nBrief unrelated mention.\n\n[4] Doc B\nAnother short passage."

_SYNTHESIS_STAGE_SUFFIX = "_fast_synthesis"


def _make_ctx():
    ctx = PipelineContext(correlation_id="fast-thin-test", thread_id=None, message="care programs?")
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.effective_message = ctx.message
    ctx.chat_mode = "quick"
    return ctx


def _run_fast_mode(rag_result_text: str, sources: list[dict], synthesis_response="Synthesized best-effort answer."):
    """synthesis_response: what the fast-mode synthesis LLM call returns.
    None simulates a failed/empty synthesis call (exercises the
    fallback-to-code-hedge path)."""
    ctx = _make_ctx()

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage == "react_1":
            return '{"thought": "search", "tool": "rag", "inputs": {"query": "care programs"}, "is_complete": false}'
        if stage.endswith(_SYNTHESIS_STAGE_SUFFIX):
            if synthesis_response is None:
                raise RuntimeError("simulated synthesis failure")
            return synthesis_response
        raise AssertionError(f"unexpected stage in fast mode (stage={stage})")

    with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
        with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
            mock_execute.return_value = {
                "tool": "rag", "success": True, "result": rag_result_text,
                "signal": "corpus_only", "sources": sources, "usage": None,
            }
            run_react(ctx, emitter=None)
    return ctx


class TestAllChunkStats:
    def test_counts_and_sums_every_chunk_unfiltered(self):
        count, chars = _all_chunk_stats(_THIN_CHUNKS)
        assert count == 2
        assert chars == len("Brief unrelated mention.") + len("Another short passage.")


class TestBuildFastModeHedge:
    """_build_fast_mode_hedge is now the FALLBACK when the synthesis
    call fails, not the primary thin-evidence path -- still tested
    directly since it's still real, load-bearing code."""

    def test_hedge_is_a_literal_excerpt_not_synthesis(self):
        hedge = _build_fast_mode_hedge(_THIN_CHUNKS, 2)
        assert "Brief unrelated mention" in hedge
        assert "Found 2 relevant passage" in hedge
        assert "Think mode" in hedge

    def test_hedge_never_crashes_on_non_chunked_text(self):
        hedge = _build_fast_mode_hedge("plain prose, no headers", 0)
        assert "Think mode" in hedge
        assert hedge.strip()


class TestRichEvidenceKeepsCurrentBehavior:
    def test_rich_evidence_ships_raw_dump_fast_no_second_round(self):
        ctx = _run_fast_mode(_RICH_CHUNKS, [{"rerank_score": 0.6}, {"rerank_score": 0.5}])
        assert ctx.final_message == _RICH_CHUNKS.strip()
        assert getattr(ctx, "react_unfinished_reason", None) is None

    def test_rich_chunk_count_and_chars_ship_directly_even_with_zero_score(self):
        """2026-08-07 (Ananth, directly, live finding): score no longer
        gates the early-exit. A 15-chunk, 11,685-char turn with
        top_score=0.00 (rerank_score simply not populated for those
        chunks) is substantial evidence regardless -- must ship
        directly, not fall into the hedge/synthesis path. This was the
        exact live regression: fast mode hedged while agentic mode,
        given the SAME evidence, produced a fuller answer."""
        ctx = _run_fast_mode(_RICH_CHUNKS, [{"rerank_score": None}, {"rerank_score": None}])
        assert ctx.final_message == _RICH_CHUNKS.strip()
        assert getattr(ctx, "react_unfinished_reason", None) is None

    def test_rich_chunk_count_and_chars_ship_directly_with_no_sources_at_all(self):
        """Same case, but sources=[] entirely (not just unscored) --
        max(..., default=0.0) must not accidentally read as "thin"."""
        ctx = _run_fast_mode(_RICH_CHUNKS, [])
        assert ctx.final_message == _RICH_CHUNKS.strip()


class TestThinEvidenceGetsSynthesizedAnswer:
    """2026-08-07 (Ananth, directly): "always let ReAct summarize, even
    on early exit." Thin evidence now gets a real synthesis attempt
    (mocked here), not just the code-constructed excerpt."""

    def test_thin_evidence_gets_synthesized_answer_plus_think_mode_line(self):
        ctx = _run_fast_mode(_THIN_CHUNKS, [{"rerank_score": 0.6}], synthesis_response="Here's a real synthesized answer from the thin evidence.")
        assert "Here's a real synthesized answer from the thin evidence." in ctx.final_message
        assert "Think mode" in ctx.final_message

    def test_hedge_path_sets_no_path_forward_for_suggest_escalate(self):
        """Reuses the EXISTING suggest_escalate signal (integrate.py reads
        react_unfinished_reason=="no_path_forward") rather than inventing
        a new mechanism -- confirms the field actually gets set even
        though the answer is now a real synthesis, not a bare hedge."""
        ctx = _run_fast_mode(_THIN_CHUNKS, [])
        assert ctx.react_unfinished_reason == "no_path_forward"

    def test_synthesis_call_receives_the_raw_evidence_and_question(self):
        captured = {}

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            if stage == "react_1":
                return '{"thought": "search", "tool": "rag", "inputs": {"query": "care programs"}, "is_complete": false}'
            captured["system"] = system
            captured["user"] = user
            return "synthesized."

        ctx = _make_ctx()
        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
                mock_execute.return_value = {
                    "tool": "rag", "success": True, "result": _THIN_CHUNKS,
                    "signal": "corpus_only", "sources": [], "usage": None,
                }
                run_react(ctx, emitter=None)

        assert "Brief unrelated mention" in captured["user"]
        assert ctx.message in captured["user"]
        assert "literally present" in captured["system"]

    def test_synthesis_failure_falls_back_to_code_hedge(self):
        """When the synthesis call raises/fails, must fall back to the
        pure code-constructed excerpt -- never crash, never ship nothing."""
        ctx = _run_fast_mode(_THIN_CHUNKS, [], synthesis_response=None)
        assert "Brief unrelated mention" in ctx.final_message
        assert "Found 2 relevant passage" in ctx.final_message
        assert ctx.react_unfinished_reason == "no_path_forward"

    def test_synthesis_never_fabricates_beyond_mocked_response(self):
        """The mocked synthesis response itself is what ships -- this
        test locks in that nothing gets appended/injected beyond the
        synthesis text + the fixed Think-mode line."""
        ctx = _run_fast_mode(_THIN_CHUNKS, [], synthesis_response="Only what the evidence says.")
        assert ctx.final_message == "Only what the evidence says.\n\nFor a more complete, verified answer, try Think mode."


class TestThresholdConstantsAreTheApprovedShape:
    def test_count_and_chars_thresholds_exist(self):
        """Score is deliberately NOT among these anymore -- decoupled
        2026-08-07 after the live zero-score false positive."""
        assert _FAST_MODE_MIN_CHUNKS >= 1
        assert _FAST_MODE_MIN_CHARS >= 1


class TestNonChunkedToolResultsBypassTheHedge:
    """2026-08-07 (Ananth, directly, live screenshot: "Can you tell me
    how to appeal for a sunshine health COB denial?"). The gate only
    makes sense for chunk-numbered rag results. A COB-appeal question
    routes to appeals_find_carc in round 1, not rag -- its raw result is
    JSON, not "[N] Doc\\ntext" chunks, so _all_chunk_stats correctly
    reads 0 chunks. Without this guard, that 0 was misinterpreted as
    "thin evidence" and real, good appeals data got routed into the
    hedge/synthesis path instead of shipping directly -- confirmed live.
    This bypass happens BEFORE the thin/rich branch, so it never reaches
    the synthesis call at all."""

    def test_non_rag_tool_result_ships_directly_not_hedged(self):
        ctx = _make_ctx()
        appeals_json = '{"matches": [{"carc": 22, "title": "Coordination of Benefits", "rules": [{"rule_id": "COB.R001"}]}], "top_carc": 22}'

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            if stage == "react_1":
                return '{"thought": "appeal", "tool": "appeals_find_carc", "inputs": {"denial_description": "COB"}, "is_complete": false}'
            raise AssertionError(f"non-chunked results must bypass synthesis entirely (stage={stage})")

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
                mock_execute.return_value = {
                    "tool": "appeals_find_carc", "success": True, "result": appeals_json,
                    "signal": None, "sources": [], "usage": None,
                }
                run_react(ctx, emitter=None)

        assert ctx.final_message == appeals_json
        assert "Found" not in ctx.final_message  # not routed through the hedge template
        assert getattr(ctx, "react_unfinished_reason", None) is None
