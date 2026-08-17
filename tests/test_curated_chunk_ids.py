"""Task #105 (2026-08-16, Chat Master/Ananth ruling): persist react's real
per-round evidence_review "keep" curation forward into ctx.curated_chunk_ids,
so integrate.py's _build_rag_chunks can use what react already judged
relevant instead of a blind top-K rerank_score cut of the full uncapped pool
(confirmed live, cid=53a99efc: that cut crowded a whole payer's evidence out
of a 4-payer comparison answer)."""
from __future__ import annotations

from app.pipeline.react_loop import (
    _curated_ids_from_kept_blocks,
    chunk_text_identity,
)
from app.stages.integrate import _build_rag_chunks


class TestChunkTextIdentity:
    def test_same_text_same_identity(self):
        assert chunk_text_identity("Provider Services: 1-844-477-8313") == \
            chunk_text_identity("Provider Services: 1-844-477-8313")

    def test_strips_whitespace(self):
        assert chunk_text_identity("  hello  ") == chunk_text_identity("hello")

    def test_truncates_long_text_consistently(self):
        long_a = "x" * 500
        long_b = "x" * 500 + "different tail"
        assert chunk_text_identity(long_a) == chunk_text_identity(long_b)


class TestCuratedIdsFromKeptBlocks:
    def test_extracts_only_kept_chunk_ids(self):
        raw = (
            "[1] Sunshine Provider Manual\nProvider phone: 1-844-477-8313\n\n"
            "[2] Aetna Guide\nAetna phone: 1-800-000-0000\n\n"
            "[3] Molina Handbook\nMolina phone: 1-555-555-5555\n"
        )
        ids = _curated_ids_from_kept_blocks(raw, keep=[1, 3])
        assert chunk_text_identity("Provider phone: 1-844-477-8313") in ids
        assert chunk_text_identity("Molina phone: 1-555-555-5555") in ids
        assert chunk_text_identity("Aetna phone: 1-800-000-0000") not in ids
        assert len(ids) == 2

    def test_empty_keep_returns_empty_set(self):
        raw = "[1] Doc\nSome text\n"
        assert _curated_ids_from_kept_blocks(raw, keep=[]) == set()

    def test_non_chunked_text_returns_empty_set(self):
        assert _curated_ids_from_kept_blocks("plain prose, no chunk markers", keep=[1]) == set()


class TestBuildRagChunksCuration:
    def _source(self, text, score, doc="Doc"):
        return {"text": text, "document_name": doc, "rerank_score": score}

    def test_prefers_curated_set_over_top_k_by_score(self):
        # 3 sources: one high-score but NOT curated, two lower-score but curated.
        high_score_uncurated = self._source("uncurated high score chunk", 0.95, "Noise")
        curated_a = self._source("curated chunk A", 0.3, "Sunshine")
        curated_b = self._source("curated chunk B", 0.2, "Molina")
        all_sources = [high_score_uncurated, curated_a, curated_b]
        curated_ids = {chunk_text_identity("curated chunk A"), chunk_text_identity("curated chunk B")}

        chunks = _build_rag_chunks(all_sources, None, "agentic", curated_chunk_ids=curated_ids)

        doc_names = [c["document_name"] for c in chunks]
        assert "Sunshine" in doc_names
        assert "Molina" in doc_names
        assert "Noise" not in doc_names

    def test_falls_back_to_top_k_when_curated_ids_empty(self):
        sources = [self._source(f"chunk {i}", score=i / 10, doc=f"Doc{i}") for i in range(10)]
        chunks = _build_rag_chunks(sources, None, "agentic", curated_chunk_ids=None)
        assert len(chunks) == 7  # original cap, unchanged

    def test_falls_back_to_top_k_when_curated_ids_match_nothing(self):
        sources = [self._source(f"chunk {i}", score=i / 10, doc=f"Doc{i}") for i in range(10)]
        chunks = _build_rag_chunks(
            sources, None, "agentic", curated_chunk_ids={"nonexistent-identity"},
        )
        assert len(chunks) == 7

    def test_quick_mode_ignores_curation_entirely(self):
        curated_a = self._source("curated chunk A", 0.1, "Sunshine")
        high_score_uncurated = self._source("uncurated high score chunk", 0.95, "Noise")
        sources = [curated_a, high_score_uncurated] + [
            self._source(f"filler {i}", score=0.5, doc=f"Filler{i}") for i in range(5)
        ]
        curated_ids = {chunk_text_identity("curated chunk A")}
        chunks = _build_rag_chunks(sources, None, "quick", curated_chunk_ids=curated_ids)
        assert len(chunks) == 4  # quick-mode cap, unchanged
        doc_names = [c["document_name"] for c in chunks]
        assert "Noise" in doc_names  # top-K by score still wins in quick mode

    def test_cap_raised_to_25_when_curated(self):
        sources = [
            self._source(f"curated {i}", score=i / 30, doc=f"Doc{i}") for i in range(30)
        ]
        curated_ids = {chunk_text_identity(f"curated {i}") for i in range(30)}
        chunks = _build_rag_chunks(sources, None, "agentic", curated_chunk_ids=curated_ids)
        assert len(chunks) == 25

    def test_ordered_by_round_then_score(self):
        # Two chunks curated from round 2 (higher score) and round 1 (lower score) --
        # round order should win: round 1's chunk comes first despite lower score.
        round1_chunk = self._source("round one chunk", score=0.2, doc="Round1")
        round2_chunk = self._source("round two chunk", score=0.9, doc="Round2")
        sources = [round2_chunk, round1_chunk]
        curated_ids = {chunk_text_identity("round one chunk"), chunk_text_identity("round two chunk")}
        evidence_memory = [
            {"call": 1, "chunk": 1, "header": "[1] Round1", "text": "round one chunk"},
            {"call": 2, "chunk": 1, "header": "[1] Round2", "text": "round two chunk"},
        ]
        chunks = _build_rag_chunks(
            sources, None, "agentic", curated_chunk_ids=curated_ids, evidence_memory=evidence_memory,
        )
        assert chunks[0]["document_name"] == "Round1"
        assert chunks[1]["document_name"] == "Round2"
