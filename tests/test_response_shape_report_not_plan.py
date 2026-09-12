"""Governor seat, 2026-09-12: v5's round-1 gaps_open shape had a real, live
side effect. Confirmed on 3/3 runs (cb12d328, 285de562, Ananth's UI turn): a
three-payer question correctly opened three gaps at round 1, then the model
narrated a sequential plan in its own "thought" field ("starting with
Molina") -- reading its own enumeration as a work queue.

v6's fix asserted a REASON for the remedy ("rag decomposes a query across
named entities internally and searches them together") that was FALSE --
Governor withdrew it after Retriever traced mobius-rag's actual dispatch
path (app/services/retriever/shape/reformat.py::_dispatch, confirmed
directly in this session too): Contour.EXACT passes straight through to one
PRECISE slot unchanged; the only fan-out trigger is UNDERSPECIFIED +
explore_siblings (lexicon-domain ambiguity, unrelated to named-entity
count). An entity-count-based auto-decomposition was tried and REVERTED
2026-07-29 (Ananth's direct call) for producing spurious facets. Governor's
"~30s broad vs ~73s narrowed" measurement mis-attributed three sequential
single-round HTTP calls to one fan-out call.

v7 keeps what IS verified (the model narrating a sequential plan is real
and undesirable) and drops the mechanism claim plus the "put every entity
in one query" remedy that depended on it -- neither "one combined query"
nor "one query per entity" is proven better without a named-entity fan-out
capability RAG does not currently have. The per-round query shape is left
to the model's own judgment.
"""
from __future__ import annotations

from app.pipeline.react.prompts import REACT_RESPONSE_SHAPE_TEXT


def test_first_round_section_says_report_not_plan():
    first_round_section = REACT_RESPONSE_SHAPE_TEXT.split("Tool call (need more evidence)")[0]
    assert "REPORT" in first_round_section
    assert "not a work queue" in first_round_section or "not a plan" in first_round_section


def test_no_claim_about_rag_decomposition_mechanism():
    # The withdrawn claim, verbatim substrings that must never reappear
    # without a real, re-verified mechanism behind them. False when
    # written, confirmed false by reading mobius-rag's actual dispatch
    # code -- Contour.EXACT is a single-slot passthrough.
    text = REACT_RESPONSE_SHAPE_TEXT
    assert "rag decomposes a query across named entities" not in text
    assert "searches them together" not in text


def test_no_remedy_prescribing_all_entities_in_one_query():
    # v6's specific, now-unsupported instruction -- removed because
    # neither "one combined query" nor "one query per entity" is proven
    # better without a fan-out capability RAG doesn't have.
    text = REACT_RESPONSE_SHAPE_TEXT
    assert "ALL THREE payers in the SAME query" not in text
    assert "your round-1 query/inputs must still cover the WHOLE" not in text


def test_avoids_narrating_a_multi_round_plan():
    # What's still verified and kept: don't narrate a sequential plan in
    # "thought" -- the specific behavior observed 3/3 times, independent
    # of which query-shaping tactic is eventually right.
    first_round_section = REACT_RESPONSE_SHAPE_TEXT.split("Tool call (need more evidence)")[0]
    assert "do not narrate a multi-round plan" in first_round_section.lower()
    assert "starting with" in first_round_section.lower()  # names the exact failure mode to avoid


def test_query_shaping_left_to_model_judgment():
    text = REACT_RESPONSE_SHAPE_TEXT
    assert "your judgment call" in text or "your call, made fresh each round" in text


def test_live_finding_is_dated_and_cited_not_asserted():
    # Matches the citation discipline used elsewhere in this codebase
    # (e.g. react.critical_rules' incident citations) -- a behavioral
    # instruction grounded in a live finding should name it, not just
    # assert the rule.
    text = REACT_RESPONSE_SHAPE_TEXT
    assert "2026-09-12" in text
