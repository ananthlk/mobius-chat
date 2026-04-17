"""Source hygiene + dedup unit tests (Phase 0.8).

Direct regression for the "1,073 citations of the same provider manual" UI bug
observed after Phases 0.6b + 0.7 + 2.5 landed — the LLM-level errors stopped
leaking, the retry guard stopped burning rounds on repeat tools, but a
rate-limited corpus search was still dumping all its pre-failure retrieved
chunks into the final source list.
"""

from __future__ import annotations

from app.pipeline.react_loop import _dedupe_sources


# ── _dedupe_sources ──────────────────────────────────────────────────────────


class TestDedupeByDocumentAndPage:
    def test_empty_returns_empty(self):
        assert _dedupe_sources([]) == []
        assert _dedupe_sources(None) == []

    def test_same_doc_same_page_collapses(self):
        a = {"document_id": "sh-manual", "page_number": 10, "title": "A"}
        b = {"document_id": "sh-manual", "page_number": 10, "title": "B"}
        out = _dedupe_sources([a, b])
        assert out == [a]  # first wins, order preserved

    def test_same_doc_different_page_kept(self):
        a = {"document_id": "sh-manual", "page_number": 10}
        b = {"document_id": "sh-manual", "page_number": 11}
        assert _dedupe_sources([a, b]) == [a, b]

    def test_regression_massive_duplicate_bloat_collapses(self):
        """Mirror the production shape: dozens of near-duplicate page-N rows."""
        bloat = []
        for page in range(1, 50):
            # Each page was cited 3x from 3 different retrieval rounds.
            for _ in range(3):
                bloat.append({
                    "document_id": "sh-manual",
                    "page_number": page,
                    "title": "Sunshine Provider Manual",
                })
        out = _dedupe_sources(bloat)
        assert len(out) == 49  # one per unique page, from 147 inputs
        # Order preserved — first page first.
        assert out[0]["page_number"] == 1
        assert out[-1]["page_number"] == 49

    def test_order_preserved_when_no_duplicates(self):
        srcs = [
            {"document_id": "a", "page_number": 1},
            {"document_id": "b", "page_number": 1},
            {"document_id": "a", "page_number": 2},
        ]
        assert _dedupe_sources(srcs) == srcs


class TestDedupeFallbackKeys:
    def test_url_dedup_when_no_document_id(self):
        a = {"url": "https://example.com/page", "title": "X"}
        b = {"url": "https://example.com/page", "title": "Y"}
        assert _dedupe_sources([a, b]) == [a]

    def test_title_dedup_when_no_doc_or_url(self):
        a = {"title": "The Sunshine Manual", "snippet": "one"}
        b = {"title": "The Sunshine Manual", "snippet": "two"}
        assert _dedupe_sources([a, b]) == [a]

    def test_different_url_kept(self):
        a = {"url": "https://example.com/p1"}
        b = {"url": "https://example.com/p2"}
        assert _dedupe_sources([a, b]) == [a, b]


class TestDedupeMixedContent:
    def test_doc_and_url_sources_mix_cleanly(self):
        corpus = {"document_id": "sh-manual", "page_number": 1}
        web = {"url": "https://sunshinehealth.com/policy"}
        dup_corpus = {"document_id": "sh-manual", "page_number": 1}
        dup_web = {"url": "https://sunshinehealth.com/policy"}
        out = _dedupe_sources([corpus, web, dup_corpus, dup_web])
        assert out == [corpus, web]

    def test_nondict_items_handled(self):
        out = _dedupe_sources(["raw string 1", "raw string 1", "raw string 2"])
        assert out == ["raw string 1", "raw string 2"]

    def test_opaque_dict_with_no_key_fields(self):
        """Dicts without doc_id/url/title fall back to content-based dedup."""
        a = {"random_key": "a"}
        b = {"random_key": "a"}  # same content → dedup
        c = {"random_key": "b"}  # different content → kept
        out = _dedupe_sources([a, b, c])
        assert len(out) == 2
