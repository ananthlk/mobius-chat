"""Fast-mode thin-evidence hedge (2026-08-07, Task #65 follow-up, Chat
Master relaying Ananth's UX contract).

The round-1 early exit in fast/"quick" mode used to ship whatever the
first rag call returned verbatim -- confirmed live (cid d288d009) as
the same fabrication mechanism #65 fixed for the normal reasoning path:
no evidence_review ran here at all, so raw, possibly-thin/mismatched
chunk text shipped as a confident-looking answer.

Ananth's three principles, all preserved by this fix:
1. Always stream something fast -- neither branch (rich or thin) adds a
   full reasoning round or an extra LLM call.
2. Never fabricate -- thin evidence gets an honest, code-CONSTRUCTED
   hedge (a literal excerpt of the retrieved text, not an LLM summary
   that could itself hallucinate).
3. Answer tab still gets the full integrator card either way -- this
   only changes what ships as react_draft/Summary, not whether
   integrate.py runs afterward (unchanged, happens in orchestrator.py
   regardless of which branch fires here).
"""
from __future__ import annotations

from unittest.mock import patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import (
    _FAST_MODE_MIN_CHARS,
    _FAST_MODE_MIN_CHUNKS,
    _FAST_MODE_MIN_SCORE,
    _all_chunk_stats,
    _build_fast_mode_hedge,
    run_react,
)

_RICH_CHUNKS = "\n\n".join(
    f"[{i}] Doc {i}\n" + ("Detailed relevant content about care management programs. " * 6)
    for i in range(1, 4)
)
_THIN_CHUNKS = "[1] Doc A\nBrief unrelated mention.\n\n[4] Doc B\nAnother short passage."


def _make_ctx():
    ctx = PipelineContext(correlation_id="fast-thin-test", thread_id=None, message="care programs?")
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.effective_message = ctx.message
    ctx.chat_mode = "quick"
    return ctx


def _run_fast_mode(rag_result_text: str, sources: list[dict]):
    ctx = _make_ctx()

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        if stage == "react_1":
            return '{"thought": "search", "tool": "rag", "inputs": {"query": "care programs"}, "is_complete": false}'
        raise AssertionError(f"fast mode must not reach a second reasoning round (stage={stage})")

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
    def test_hedge_is_a_literal_excerpt_not_synthesis(self):
        hedge = _build_fast_mode_hedge(_THIN_CHUNKS, 2)
        assert "Brief unrelated mention" in hedge
        assert "Limited sources" in hedge
        assert "Think mode" in hedge

    def test_hedge_never_crashes_on_non_chunked_text(self):
        hedge = _build_fast_mode_hedge("plain prose, no headers", 0)
        assert "Limited sources" in hedge
        assert "Think mode" in hedge


class TestRichEvidenceKeepsCurrentBehavior:
    def test_rich_evidence_ships_raw_dump_fast_no_second_round(self):
        ctx = _run_fast_mode(_RICH_CHUNKS, [{"rerank_score": 0.6}, {"rerank_score": 0.5}])
        assert ctx.final_message == _RICH_CHUNKS.strip()
        assert getattr(ctx, "react_unfinished_reason", None) is None


class TestThinEvidenceGetsHonestHedge:
    def test_thin_chunk_count_triggers_hedge(self):
        """Only 2 chunks, below _FAST_MODE_MIN_CHUNKS=3."""
        ctx = _run_fast_mode(_THIN_CHUNKS, [{"rerank_score": 0.6}])
        assert ctx.final_message != _THIN_CHUNKS
        assert "Limited sources" in ctx.final_message
        assert "Think mode" in ctx.final_message

    def test_thin_score_triggers_hedge_even_with_enough_chunks_and_chars(self):
        """Rich chunk count/chars but low relevance score -- must still hedge."""
        ctx = _run_fast_mode(_RICH_CHUNKS, [{"rerank_score": 0.1}, {"rerank_score": 0.05}])
        assert "Limited sources" in ctx.final_message

    def test_hedge_path_sets_no_path_forward_for_suggest_escalate(self):
        """Reuses the EXISTING suggest_escalate signal (integrate.py reads
        react_unfinished_reason=="no_path_forward") rather than inventing
        a new mechanism -- confirms the field actually gets set."""
        ctx = _run_fast_mode(_THIN_CHUNKS, [])
        assert ctx.react_unfinished_reason == "no_path_forward"

    def test_hedge_never_fabricates_beyond_the_retrieved_text(self):
        """The hedge for thin evidence must not contain content that
        isn't literally present in what was retrieved -- e.g. no
        invented program names or numbers."""
        ctx = _run_fast_mode(_THIN_CHUNKS, [])
        assert "MMA" not in ctx.final_message
        assert "Comprehensive program" not in ctx.final_message


class TestThresholdConstantsAreTheApprovedShape:
    def test_all_three_signals_exist(self):
        assert _FAST_MODE_MIN_CHUNKS >= 1
        assert _FAST_MODE_MIN_CHARS >= 1
        assert 0.0 < _FAST_MODE_MIN_SCORE < 1.0
