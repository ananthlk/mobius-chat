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


class TestTheFactsSchemaReachesTheGovernorLoop:
    """The starvation upstream of the mapper.

    `system_suffix` carries the "facts": [{fact, document, page}] block, and
    its only caller was react_loop.py:7171 -- v1's loop. The governor loop
    never appended it, so the model was never ASKED for provenance. It
    answered with facts that had none, and three consumers reported that
    honestly without saying why:

        verify:    "skipped=no facts with a document and page"
        contract:  "fact with no document"
        citations: [] beside thirteen published sources
    """

    def test_the_governor_loop_appends_the_v2_system_suffix(self):
        """Asserted on the SOURCE, because the alternative is a live turn.

        Parse rather than grep: a match inside a comment or a docstring is
        exactly how this file has produced a false green before.
        """
        import ast
        import pathlib

        import app.pipeline.v2.loop as L

        tree = ast.parse(pathlib.Path(L.__file__).read_text())
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "system_suffix"
        ]
        assert calls, (
            "the governor loop never calls system_suffix, so the model is "
            "never asked which document a fact came from"
        )

    def test_the_suffix_actually_asks_for_the_document(self):
        """The call is worth nothing if the block it appends dropped the
        field. Assert the PROPERTY -- provenance is requested -- not that a
        particular function was called."""
        from types import SimpleNamespace

        from app.pipeline.v2 import prompts as P

        suffix = P.system_suffix(SimpleNamespace(orchestrator_version="v2"))
        assert suffix, "v2 got an empty suffix"
        flat = " ".join(suffix.split())
        assert '"facts"' in flat
        assert '"document"' in flat, "facts are requested without provenance"
        assert '"page"' in flat

    def test_v1_is_not_handed_a_v2_suffix(self):
        """v1 is the A/B control arm. A v2 module must never hand it a value
        it chose -- that moves the control and destroys the comparison."""
        from types import SimpleNamespace

        from app.pipeline.v2 import prompts as P

        assert P.system_suffix(SimpleNamespace(orchestrator_version="v1")) == ""
