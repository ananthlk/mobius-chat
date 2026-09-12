"""Governor seat, 2026-09-12: v5's round-1 gaps_open shape (test_response_
shape_round1_gaps.py) had a real, live side effect nobody intended. Confirmed
on 3/3 runs (cb12d328, 285de562, Ananth's UI turn): a three-payer question
correctly opened three gaps at round 1, then the model narrowed its own tool
call to the first entity, stating "starting with Molina" -- reading its own
enumeration as a work queue. rag's own slot decomposition (mobius-rag
orchestrator.py:786-828, confirmed by Governor with line numbers) already
covers all named entities concurrently when the query names them; narrowing
to one entity per round costs real time (measured same corpus/hour: ~30s
broad vs ~73s narrowed-then-re-expanded).

Not a revert -- Ananth's ruling was against round-1 SEQUENCING the search,
not against naming the parts (which is what feeds the governor's round-2+
closure ledger with real gaps). v6 makes the list an explicit REPORT, not a
plan, and gives a positive worked example showing the query naming every
part gaps_open lists.
"""
from __future__ import annotations

from app.pipeline.react.prompts import REACT_RESPONSE_SHAPE_TEXT


def test_first_round_section_says_report_not_plan():
    first_round_section = REACT_RESPONSE_SHAPE_TEXT.split("Tool call (need more evidence)")[0]
    assert "REPORT" in first_round_section
    assert "not a work queue" in first_round_section or "not a plan" in first_round_section


def test_multi_part_example_names_every_entity_in_the_query_not_one():
    # The positive example must show ALL THREE payers in the query/inputs
    # description, not just the gaps_open list in isolation -- a list with
    # no example of the correct tool call left the model to guess, and it
    # guessed "start with the first one."
    first_round_section = REACT_RESPONSE_SHAPE_TEXT.split("Tool call (need more evidence)")[0]
    multi_part_example = first_round_section[first_round_section.index("Example — multi-part"):]
    normalized = " ".join(multi_part_example.split())  # collapse the block's own line-wrapping
    assert "ALL THREE" in normalized
    assert "starting with" in normalized.lower()  # names the exact failure mode to avoid


def test_live_finding_is_dated_and_cited_not_asserted():
    # Matches the citation discipline used elsewhere in this codebase
    # (e.g. react.critical_rules' incident citations) -- a behavioral
    # instruction grounded in a live finding should name it, not just
    # assert the rule.
    text = REACT_RESPONSE_SHAPE_TEXT
    assert "2026-09-12" in text
