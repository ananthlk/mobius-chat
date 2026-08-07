"""Tests for the FACTUAL/BLENDED collapse into one unified 'answer' consolidator
path (2026-08-07 architecture directive) -- CANONICAL stays distinct."""
from __future__ import annotations

from app.responder.final import choose_consolidator_type


def test_choose_consolidator_type_low_score_is_answer():
    assert choose_consolidator_type(0.1, factual_max=0.4, canonical_min=0.6) == "answer"


def test_choose_consolidator_type_mid_score_is_answer():
    """Scores that used to land on 'blended' now collapse into 'answer' too."""
    assert choose_consolidator_type(0.5, factual_max=0.4, canonical_min=0.6) == "answer"


def test_choose_consolidator_type_high_score_stays_canonical():
    assert choose_consolidator_type(0.9, factual_max=0.4, canonical_min=0.6) == "canonical"


def test_choose_consolidator_type_boundary_at_canonical_min_is_answer():
    """canonical_min itself is NOT '> canonical_min' -- stays answer at the boundary,
    matching the pre-merge behavior where the boundary landed on 'blended'."""
    assert choose_consolidator_type(0.6, factual_max=0.4, canonical_min=0.6) == "answer"


def test_choose_consolidator_type_only_two_values_possible():
    for score in (0.0, 0.2, 0.4, 0.5, 0.6, 0.7, 1.0):
        result = choose_consolidator_type(score, factual_max=0.4, canonical_min=0.6)
        assert result in ("answer", "canonical")
