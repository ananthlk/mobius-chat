"""Phase 0.18 — confidence_min filter respects RAG API chunk shape.

Regression test for the silent retrieval-killer found in the 2026-04-17
live test:

    16:28:30  after normalize: type=list len=5   ← RAG API returned 5 chunks
    16:28:32  before context build: type=list len=0   ← ZERO reached the LLM

Root cause: ``confidence_min=0.5`` filter in non_patient_rag.py checked
``match_score`` and ``confidence`` fields only. Chunks from the RAG API
path carry ``rerank_score`` + ``confidence_label`` instead — so every
one of them scored as 0.0 and got filtered out. Every turn pivoted to
google_search + web_scrape as if the corpus were empty.

Phase 0.18 introduces ``_score_chunk_for_confidence_filter`` that falls
back through multiple numeric fields and finally a label-to-numeric
mapping. This test suite locks that in.
"""

from __future__ import annotations

import pytest

from app.services.non_patient_rag import (
    _CONFIDENCE_LABEL_SCORE,
    _score_chunk_for_confidence_filter,
)


# ── Legacy BM25 shape (pre-0.18 behavior must be preserved) ────────────────


class TestLegacyBM25Shape:
    def test_match_score_preferred(self):
        c = {"match_score": 0.72, "confidence": 0.3}
        assert _score_chunk_for_confidence_filter(c) == 0.72

    def test_confidence_used_when_match_score_missing(self):
        c = {"confidence": 0.55}
        assert _score_chunk_for_confidence_filter(c) == 0.55

    def test_numeric_zero_still_zero(self):
        """match_score=0 is a real "no match" — not a reason to fall through
        to the next field."""
        c = {"match_score": 0.0, "confidence": 0.9}
        assert _score_chunk_for_confidence_filter(c) == 0.0


# ── RAG API shape (the bug this phase fixes) ────────────────────────────────


class TestRagApiShape:
    def test_rerank_score_used(self):
        """Exact chunk shape observed in the 2026-04-17 production log."""
        c = {
            "text": "H0036 is …",
            "document_name": "Sunshine Provider Manual",
            "source_type": "chunk",
            "confidence_label": "process_confident",
            "llm_guidance": "…",
            "rerank_score": 0.87,
        }
        assert _score_chunk_for_confidence_filter(c) == 0.87

    def test_canonical_process_confident_clears_default(self):
        """REGRESSION for the silent-filter bug. The RAG API emits
        ``process_confident`` for high-score chunks; must clear 0.5.
        """
        c = {"confidence_label": "process_confident"}
        assert _score_chunk_for_confidence_filter(c) >= 0.5

    def test_canonical_process_with_caution_clears_default(self):
        """Middle-tier chunks should still reach the LLM at 0.5 threshold."""
        c = {"confidence_label": "process_with_caution"}
        assert _score_chunk_for_confidence_filter(c) >= 0.5

    def test_canonical_abstain_below_default(self):
        """``abstain`` chunks (rerank < 0.5) are filtered at the default threshold."""
        c = {"confidence_label": "abstain"}
        assert _score_chunk_for_confidence_filter(c) < 0.5

    def test_confidence_label_fallback_when_no_numeric_fields(self):
        c = {"confidence_label": "informational"}
        s = _score_chunk_for_confidence_filter(c)
        assert s >= 0.5, (
            f"'informational' should clear the default 0.5 filter; got {s}"
        )

    def test_high_label_clears_default_threshold(self):
        c = {"confidence_label": "high"}
        assert _score_chunk_for_confidence_filter(c) >= 0.5

    def test_low_label_is_below_default_threshold(self):
        c = {"confidence_label": "low"}
        assert _score_chunk_for_confidence_filter(c) < 0.5

    def test_unknown_label_returns_zero(self):
        c = {"confidence_label": "random_new_label_we_never_saw"}
        assert _score_chunk_for_confidence_filter(c) == 0.0

    def test_empty_label_returns_zero(self):
        c = {"confidence_label": ""}
        assert _score_chunk_for_confidence_filter(c) == 0.0

    def test_label_is_case_insensitive(self):
        c1 = {"confidence_label": "PROCESS_CONFIDENT"}
        c2 = {"confidence_label": "process_confident"}
        assert _score_chunk_for_confidence_filter(c1) == _score_chunk_for_confidence_filter(c2)


# ── Regression against the exact 2026-04-17 live-test bug ──────────────────


class TestProductionRegressionOf20260417:
    """The exact sequence that triggered this phase. Five chunks from RAG
    API, every one with rerank_score but no match_score/confidence.
    Pre-0.18 filter dropped all five to 0.0. Post-0.18 all five survive.
    """

    def _sample_chunks(self) -> list[dict]:
        # Minimal reproduction of the real chunk shape from the worker log.
        # Uses the CANONICAL ``process_confident`` label that doc_assembly
        # actually emits (not the ``"high"`` label I first mistakenly assumed).
        return [
            {
                "text": f"chunk text {i}",
                "document_name": "Sunshine Provider Manual",
                "source_type": "chunk",
                "confidence_label": "process_confident",
                "llm_guidance": "Use this chunk.",
                "rerank_score": 0.7 + (i * 0.03),
            }
            for i in range(5)
        ]

    def test_pre_0_18_filter_would_drop_all(self):
        """Asserts the OLD behavior to document the bug — uses the legacy
        predicate inline so this test doesn't depend on any pre-0.18
        code path still existing.
        """
        confidence_min = 0.5
        chunks = self._sample_chunks()
        # Old predicate: only considered match_score or confidence.
        surviving = [
            c for c in chunks
            if (c.get("match_score") or c.get("confidence") or 0.0) >= confidence_min
        ]
        assert len(surviving) == 0, (
            "pre-0.18 filter was supposed to drop all 5 — if it doesn't, "
            "the chunk shape used in this test doesn't reproduce the bug"
        )

    def test_post_0_18_filter_keeps_all(self):
        """With the new scoring helper, all 5 chunks clear the 0.5 threshold."""
        confidence_min = 0.5
        chunks = self._sample_chunks()
        surviving = [
            c for c in chunks
            if _score_chunk_for_confidence_filter(c) >= confidence_min
        ]
        assert len(surviving) == 5, (
            f"post-0.18 filter should keep all 5 RAG-API chunks, got {len(surviving)}"
        )


# ── Label-score table sanity ───────────────────────────────────────────────


class TestLabelScoreTable:
    def test_all_known_labels_are_numeric(self):
        for label, score in _CONFIDENCE_LABEL_SCORE.items():
            assert 0.0 <= score <= 1.0, (
                f"label {label!r} mapped to {score} — must be in [0.0, 1.0]"
            )

    def test_high_confidence_variants_above_medium(self):
        """Sanity: high/authoritative-style labels rank above medium."""
        hi = _CONFIDENCE_LABEL_SCORE["high"]
        med = _CONFIDENCE_LABEL_SCORE["medium"]
        authoritative = _CONFIDENCE_LABEL_SCORE["authoritative"]
        assert authoritative >= hi > med, (
            "label order regressed: authoritative >= high > medium expected, "
            f"got authoritative={authoritative}, high={hi}, medium={med}"
        )

    def test_default_threshold_admits_informational(self):
        assert _CONFIDENCE_LABEL_SCORE["informational"] >= 0.5

    def test_canonical_labels_order(self):
        """The three canonical doc_assembly labels must rank in the expected
        order: process_confident > process_with_caution > abstain.
        """
        conf = _CONFIDENCE_LABEL_SCORE["process_confident"]
        caut = _CONFIDENCE_LABEL_SCORE["process_with_caution"]
        absta = _CONFIDENCE_LABEL_SCORE["abstain"]
        assert conf > caut > absta, (
            f"canonical label order regressed: got process_confident={conf}, "
            f"process_with_caution={caut}, abstain={absta}"
        )

    def test_canonical_labels_straddle_default_threshold(self):
        """process_with_caution admits at 0.5; abstain rejects at 0.5.
        This is what makes the default filter semantically right.
        """
        assert _CONFIDENCE_LABEL_SCORE["process_with_caution"] >= 0.5
        assert _CONFIDENCE_LABEL_SCORE["abstain"] < 0.5
