"""v2 publishes sources and cites none of them.

`cited_source_indices` is written by the parallel CRITIC (Call B). The v2
card is built deterministically, which skips Call A and Call B together, so
the field kept the `[]` that orchestrator.py initialises it to while
thirteen sources shipped beside it -- measured on live turn e92e934e.

A consumer reading a field nobody fills. The fix derives it from facts we
were already handed rather than re-running a model to recover them.
"""
import pytest

from app.pipeline.v2.contract import Fact
from app.responder.v2_adapter import cited_indices_from_facts


def _src(index, document_id, page=None):
    return {"index": index, "document_id": document_id, "page_number": page,
            "document_name": f"doc-{document_id}", "text": "x" * 40}


class TestTheCitationIsDerivedNotGuessed:
    def test_a_fact_cites_the_document_it_came_from(self):
        srcs = [_src(1, "A"), _src(2, "B"), _src(3, "A")]
        assert cited_indices_from_facts([Fact("f", "DocA", None, "A")], srcs) == [1, 3]

    def test_a_page_narrows_the_citation_when_the_rows_carry_one(self):
        srcs = [_src(1, "A", 3), _src(2, "A", 7)]
        assert cited_indices_from_facts([Fact("f", "DocA", 7, "A")], srcs) == [2]

    def test_a_page_that_matches_nothing_falls_back_to_the_document(self):
        """Honest breadth beats a false precision: the document DOES support
        the claim even when we cannot say which chunk of it did."""
        srcs = [_src(1, "A", 3), _src(2, "A", 7)]
        assert cited_indices_from_facts([Fact("f", "DocA", 99, "A")], srcs) == [1, 2]

    def test_indices_are_sorted_and_deduplicated(self):
        srcs = [_src(3, "A"), _src(1, "A")]
        facts = [Fact("f1", "DocA", None, "A"), Fact("f2", "DocA", None, "A")]
        assert cited_indices_from_facts(facts, srcs) == [1, 3]


class TestItRefusesRatherThanGuesses:
    def test_a_fact_with_no_resolved_document_id_cites_nothing(self):
        """Not evidence the document is absent -- evidence we could not point
        at it. A citation we cannot make is not a citation we may invent."""
        srcs = [_src(1, "A")]
        assert cited_indices_from_facts([Fact("f", "DocA", None, "")], srcs) == []

    def test_it_never_matches_on_a_similar_NAME(self):
        """NO FUZZY MATCHING -- the rule with_document_ids states, for the
        same reason: a claim pointed at the wrong document cites the wrong
        text, and a confident wrong citation is worse than an absent one."""
        srcs = [_src(1, "corpus-id-A")]
        # identical document NAME, different corpus id
        assert cited_indices_from_facts([Fact("f", "doc-corpus-id-A", None, "other")], srcs) == []

    def test_no_sources_and_no_facts_are_both_empty_not_errors(self):
        assert cited_indices_from_facts([Fact("f", "D", None, "A")], []) == []
        assert cited_indices_from_facts((), [_src(1, "A")]) == []
        assert cited_indices_from_facts(None, None) == []


def test_the_live_shape_produces_citations():
    """Built from the real turn's source shape (13 rows, 1-based `index`,
    repeated document_ids across chunks) rather than an invented one."""
    srcs = [_src(i, did) for i, did in enumerate(
        ["69e4d10b", "d9721756", "3e20da1e", "d9721756", "3e20da1e"], start=1)]
    got = cited_indices_from_facts([Fact("deadline is 90 days", "Sunshine Provider Manual",
                                         None, "d9721756")], srcs)
    assert got == [2, 4], f"expected both Sunshine chunks, got {got}"
