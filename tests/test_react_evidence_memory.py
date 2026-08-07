"""Evidence memory + evidence_review (2026-08-07, Ananth, directly).

Companion to the no-truncation fix (test_react_no_truncation.py): once
every chunk of every rag call is fully visible every round, context
grows every round too. react now actively curates via evidence_review
("keep": [chunk numbers], "running_answer", "gaps") instead of a blind
string slice deciding for it. Chunks it doesn't keep are not deleted --
they're stashed in ctx._evidence_memory and recallable via the
recall_evidence tool without spending a rag-budget slot.
"""

from __future__ import annotations

from app.pipeline.context import PipelineContext
from app.pipeline.react.prompts import build_reasoning_context
from app.pipeline.react_loop import (
    _execute_tool,
    _extract_chunk_blocks,
    _prune_kept_chunks,
    _store_evidence_memory,
)

_THREE_CHUNKS = (
    "[1] Doc A\nirrelevant chunk one text\n\n"
    "[2] Doc B\nTHE-ANSWER-180-DAYS chunk two text\n\n"
    "[3] Doc C\nirrelevant chunk three text"
)


def _make_ctx() -> PipelineContext:
    ctx = PipelineContext(correlation_id="mem-test", thread_id=None, message="q")
    ctx.effective_message = ctx.message
    return ctx


class TestExtractChunkBlocks:
    def test_splits_numbered_chunks(self):
        blocks = _extract_chunk_blocks(_THREE_CHUNKS)
        assert [b[0] for b in blocks] == [1, 2, 3]
        assert "THE-ANSWER-180-DAYS" in blocks[1][1]

    def test_non_chunked_text_returns_empty(self):
        assert _extract_chunk_blocks("plain NPPES prose, no headers") == []

    def test_empty_string_returns_empty(self):
        assert _extract_chunk_blocks("") == []


class TestPruneKeptChunks:
    def test_keeps_only_listed_chunks(self):
        out = _prune_kept_chunks(_THREE_CHUNKS, [2], call_idx=1)
        assert "THE-ANSWER-180-DAYS" in out
        assert "irrelevant chunk one" not in out
        assert "irrelevant chunk three" not in out

    def test_set_aside_chunks_get_recallable_ref_not_deleted_silently(self):
        out = _prune_kept_chunks(_THREE_CHUNKS, [2], call_idx=5)
        assert "5.1" in out
        assert "5.3" in out
        assert "recall_evidence" in out

    def test_bad_keep_list_falls_back_to_original_not_destroyed(self):
        """A keep list that matches nothing real (e.g. hallucinated chunk
        numbers) must never wipe out the evidence -- fail safe to the
        untouched original."""
        out = _prune_kept_chunks(_THREE_CHUNKS, [99], call_idx=1)
        assert out == _THREE_CHUNKS

    def test_non_chunked_text_untouched(self):
        raw = "NPPES prose with no chunk headers at all"
        assert _prune_kept_chunks(raw, [1], call_idx=1) == raw


class TestStoreEvidenceMemory:
    def test_stores_every_chunk_before_any_pruning(self):
        ctx = _make_ctx()
        _store_evidence_memory(ctx, 3, _THREE_CHUNKS)
        mem = ctx._evidence_memory
        assert len(mem) == 3
        assert {m["chunk"] for m in mem} == {1, 2, 3}
        assert all(m["call"] == 3 for m in mem)
        assert "THE-ANSWER-180-DAYS" in next(m["text"] for m in mem if m["chunk"] == 2)

    def test_accumulates_across_multiple_calls(self):
        ctx = _make_ctx()
        _store_evidence_memory(ctx, 1, _THREE_CHUNKS)
        _store_evidence_memory(ctx, 2, "[1] Doc D\nround two chunk text")
        assert len(ctx._evidence_memory) == 4
        assert {(m["call"], m["chunk"]) for m in ctx._evidence_memory} == {
            (1, 1), (1, 2), (1, 3), (2, 1),
        }

    def test_non_chunked_result_stores_nothing(self):
        ctx = _make_ctx()
        _store_evidence_memory(ctx, 1, "plain prose result")
        assert getattr(ctx, "_evidence_memory", None) in (None, [])


class TestRecallEvidenceTool:
    def test_recalls_a_stored_chunk_by_ref(self):
        ctx = _make_ctx()
        _store_evidence_memory(ctx, 2, _THREE_CHUNKS)
        r = _execute_tool("recall_evidence", {"refs": ["2.2"]}, ctx, None)
        assert r["success"] is True
        assert "THE-ANSWER-180-DAYS" in r["result"]

    def test_unknown_ref_reports_missing_not_a_crash(self):
        ctx = _make_ctx()
        _store_evidence_memory(ctx, 1, _THREE_CHUNKS)
        r = _execute_tool("recall_evidence", {"refs": ["9.9"]}, ctx, None)
        assert r["success"] is False
        assert "9.9" in r["result"]

    def test_no_memory_yet_does_not_crash(self):
        ctx = _make_ctx()
        r = _execute_tool("recall_evidence", {"refs": ["1.1"]}, ctx, None)
        assert r["success"] is False

    def test_mixed_found_and_missing_refs(self):
        ctx = _make_ctx()
        _store_evidence_memory(ctx, 1, _THREE_CHUNKS)
        r = _execute_tool("recall_evidence", {"refs": ["1.2", "9.9"]}, ctx, None)
        assert r["success"] is True
        assert "THE-ANSWER-180-DAYS" in r["result"]
        assert "9.9" in r["result"]


class TestEvidenceReviewInReasoningContext:
    def test_latest_running_answer_and_gaps_rendered(self):
        ctx = _make_ctx()
        out = build_reasoning_context(
            ctx, [], 2,
            evidence_review_latest={
                "running_answer": "180 days for initial claims",
                "gaps_open": ["corrected claim window"],
                "gaps_closed": ["initial filing deadline"],
            },
        )
        assert "180 days for initial claims" in out
        assert "corrected claim window" in out
        assert "initial filing deadline" in out
        assert "[Evidence Review" in out

    def test_falls_back_to_ctx_attribute_when_not_passed(self):
        ctx = _make_ctx()
        ctx._evidence_review_latest = {"running_answer": "found it", "gaps_open": [], "gaps_closed": []}
        out = build_reasoning_context(ctx, [], 2)
        assert "found it" in out

    def test_absent_when_nothing_set(self):
        ctx = _make_ctx()
        out = build_reasoning_context(ctx, [], 1)
        assert "[Evidence Review" not in out


class TestResponseShapeDocumentsEvidenceReview:
    def test_schema_mentions_keep_running_answer_gaps(self):
        from app.pipeline.react.prompts import REACT_RESPONSE_SHAPE_TEXT
        assert '"keep"' in REACT_RESPONSE_SHAPE_TEXT
        assert '"running_answer"' in REACT_RESPONSE_SHAPE_TEXT
        assert '"gaps_closed"' in REACT_RESPONSE_SHAPE_TEXT
        assert '"gaps_open"' in REACT_RESPONSE_SHAPE_TEXT

    def test_manifest_documents_recall_evidence(self):
        from app.pipeline.tool_manifest import get_tool_manifest
        manifest = get_tool_manifest()
        assert "recall_evidence" in manifest
        assert "call.chunk" in manifest or "no rag budget" in manifest
