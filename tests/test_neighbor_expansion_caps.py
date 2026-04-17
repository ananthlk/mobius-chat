"""Phase 0.11 — neighbor-expansion sanity.

Direct regression for the "20-seed retrieval ballooned to 1,078 sources" UI bug.
The diagnosis: ``_fetch_sibling_paragraphs`` queried ``paragraph_index BETWEEN
lo AND hi`` with no page constraint, but ``paragraph_index`` is not globally
unique per document — it appears to reset per page. So a ±2 window was
matching ~5 rows on every page of a 139-page manual.

Fix:
1. Page-constrained sibling fetch (``page_number BETWEEN seed_page ± 1``) —
   still captures "last line of page N continues on page N+1" while killing
   the per-page-reset explosion.
2. Defense-in-depth caps: max 50 chunks total, max 8 per document.
3. Citation index renumbering so the UI shows [1][2][3]… consecutively.
"""

from __future__ import annotations

from app.services.doc_assembly import (
    NEIGHBOR_PER_DOC_CAP,
    NEIGHBOR_TOTAL_CAP,
    _apply_chunk_caps,
)
from app.pipeline.react_loop import _dedupe_sources


# ── _apply_chunk_caps ───────────────────────────────────────────────────────


def _seed(doc: str, page: int, idx: int, score: float = 0.8) -> dict:
    return {
        "id": f"{doc}-p{page}-i{idx}",
        "document_id": doc,
        "document_name": f"{doc}.pdf",
        "page_number": page,
        "paragraph_index": idx,
        "match_score": score,
        "text": f"seed p{page}-{idx}",
    }


def _neighbor(doc: str, page: int, idx: int, score: float = 0.3) -> dict:
    r = _seed(doc, page, idx, score)
    r["is_neighbor"] = True
    return r


class TestTotalCap:
    def test_total_cap_enforced(self):
        """200 chunks spread across many docs → at most NEIGHBOR_TOTAL_CAP after cap.

        Use distinct docs so per-doc cap doesn't fire before the total cap
        — this isolates the total-cap behavior from the per-doc cap.
        """
        seeds = [_seed(f"d{i}", 0, 0) for i in range(200)]
        out = _apply_chunk_caps(seeds)
        assert len(out) == NEIGHBOR_TOTAL_CAP

    def test_no_cap_applied_when_under_limit(self):
        """10 chunks across 10 distinct docs → all 10 survive (under both caps)."""
        seeds = [_seed(f"d{i}", 0, 0) for i in range(10)]
        out = _apply_chunk_caps(seeds)
        assert len(out) == 10


class TestPerDocCap:
    def test_single_doc_capped_per_doc(self):
        """30 chunks from one doc → capped to NEIGHBOR_PER_DOC_CAP."""
        chunks = [_seed("d1", p, 0) for p in range(30)]
        out = _apply_chunk_caps(chunks)
        assert len(out) == NEIGHBOR_PER_DOC_CAP

    def test_multiple_docs_each_get_their_share(self):
        """5 docs × 20 chunks each → each doc capped to PER_DOC_CAP."""
        chunks = [_seed(f"d{doc}", p, 0) for doc in range(5) for p in range(20)]
        out = _apply_chunk_caps(chunks)
        by_doc: dict[str, int] = {}
        for c in out:
            by_doc[c["document_id"]] = by_doc.get(c["document_id"], 0) + 1
        for doc, n in by_doc.items():
            assert n <= NEIGHBOR_PER_DOC_CAP, f"{doc} has {n} chunks, cap is {NEIGHBOR_PER_DOC_CAP}"

    def test_regression_20_seed_plus_neighbors_stays_small(self):
        """The exact prod pathology: 20 seeds of one doc each with 50 'neighbors'
        (simulating the pre-fix behavior) → cap kicks in and output stays ≤ 50.
        """
        chunks: list[dict] = []
        for seed_page in range(20):
            chunks.append(_seed("sunshine-manual", seed_page, 0, score=0.9))
            for n_page in range(50):
                chunks.append(_neighbor("sunshine-manual", n_page, 3, score=0.4))
        out = _apply_chunk_caps(chunks)
        assert len(out) <= NEIGHBOR_TOTAL_CAP, (
            f"Post-cap output has {len(out)} chunks — regression back to the "
            f"pre-0.11 explosion shape"
        )
        # Single-doc cap: the whole batch is one doc, so ≤ PER_DOC_CAP.
        assert len(out) <= NEIGHBOR_PER_DOC_CAP


class TestSeedsPrioritized:
    def test_seeds_kept_before_neighbors(self):
        """Within the cap, seeds retrieval order takes priority over neighbors."""
        seeds = [_seed("d1", p, 0, score=0.9) for p in range(5)]
        neighbors = [_neighbor("d1", p, 1, score=0.4) for p in range(20)]
        # Mixed input: neighbors first to prove we're sorting, not preserving input order
        out = _apply_chunk_caps(neighbors + seeds)
        # All seeds should survive since count (5) < per_doc_cap (8).
        kept_seeds = [c for c in out if not c.get("is_neighbor")]
        assert len(kept_seeds) == 5

    def test_neighbors_sorted_by_score_desc(self):
        """Among neighbors, higher match_score wins."""
        seeds = [_seed("d1", 0, 0)]
        neighbors = [
            _neighbor("d1", 0, i, score=0.1 * i) for i in range(1, 15)
        ]
        out = _apply_chunk_caps(seeds + neighbors)
        # Expect: 1 seed + up to (PER_DOC_CAP - 1) neighbors, highest scores first.
        kept_neighbors = [c for c in out if c.get("is_neighbor")]
        scores = [c["match_score"] for c in kept_neighbors]
        assert scores == sorted(scores, reverse=True), "neighbors must be score-sorted"


# ── _dedupe_sources renumbering (Phase 0.11 cosmetic fix) ───────────────────


class TestCitationRenumbering:
    def test_gaps_collapsed_to_consecutive(self):
        """Bug: pre-0.11 the UI showed [1] [2] [3] [5] [7] [10] because dedup
        dropped 4, 6, 8, 9 but surviving entries kept their old ``index``.
        Fix: renumber the ``index`` field so the UI shows [1] [2] [3] [4] [5] [6].
        """
        sources = [
            {"index": 1, "document_id": "d1", "page_number": 1},
            {"index": 2, "document_id": "d1", "page_number": 2},
            {"index": 3, "document_id": "d1", "page_number": 3},
            {"index": 4, "document_id": "d1", "page_number": 1},  # dup of idx 1
            {"index": 5, "document_id": "d1", "page_number": 5},
            {"index": 6, "document_id": "d1", "page_number": 2},  # dup of idx 2
            {"index": 7, "document_id": "d1", "page_number": 7},
        ]
        out = _dedupe_sources(sources)
        indices = [s["index"] for s in out]
        assert indices == [1, 2, 3, 4, 5], (
            f"Post-dedup indices should be consecutive, got {indices}"
        )

    def test_non_dict_sources_unaffected_by_renumbering(self):
        """String sources don't have ``index``, so nothing to renumber — they
        just survive dedup."""
        sources = ["a", "a", "b"]
        out = _dedupe_sources(sources)
        assert out == ["a", "b"]

    def test_dicts_without_existing_index_left_alone(self):
        """Dicts that never had an ``index`` field don't acquire one post-dedup."""
        sources = [{"document_id": "d1", "page_number": 1}]
        out = _dedupe_sources(sources)
        assert "index" not in out[0]
