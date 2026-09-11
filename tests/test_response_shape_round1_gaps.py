"""Governor work order (docs/work-order-round1-decomposition.md), 2026-09-11:
round 1 gets its own minimal evidence_review shape (gaps_open only) so the
governor's spendable() budget arithmetic stops working off a wrong gap count
on multi-part questions -- measured: every R1 in a 20-question run had
exactly one gap, up to 4 by round 3, on 6 of 20 turns.

Traced before writing: no existing decomposition producer in react
(app.planner.schemas.Plan/SubQuestion is a degenerate single-item shim, not
real decomposition) -- this is genuinely new signal.
"""
from __future__ import annotations

from app.pipeline.react.prompts import REACT_RESPONSE_SHAPE_TEXT


def test_first_round_shape_asks_for_gaps_open_only():
    assert "First round" in REACT_RESPONSE_SHAPE_TEXT
    # Must NOT ask for the fields that are meaningless with no evidence yet.
    first_round_section = REACT_RESPONSE_SHAPE_TEXT.split("Tool call (need more evidence)")[0]
    assert '"gaps_open"' in first_round_section
    assert '"keep"' not in first_round_section
    assert '"running_answer"' not in first_round_section
    assert '"gaps_closed"' not in first_round_section


def test_single_part_is_the_primary_example():
    # feedback_example_undermines_the_rule: whatever example is shown gets
    # copied. The single-part (empty array) case must appear before the
    # multi-part case, not after it.
    text = REACT_RESPONSE_SHAPE_TEXT
    single_part_idx = text.index("Example — single-part")
    multi_part_idx = text.index("Example — multi-part")
    assert single_part_idx < multi_part_idx


def test_round2_plus_shapes_are_byte_preserved():
    # The existing tool-call and final-answer shapes (round 2+) must be
    # untouched -- this change only adds a new first-round shape.
    text = REACT_RESPONSE_SHAPE_TEXT
    assert (
        'Tool call (need more evidence) — include "evidence_review" whenever this is NOT your first\n'
        'round (i.e. earlier tool results are present in context above):'
    ) in text
    assert (
        'Final answer (have enough evidence to answer now) — include "evidence_review" whenever this is\n'
        'NOT your first round, same as the tool-call shape.'
    ) in text
