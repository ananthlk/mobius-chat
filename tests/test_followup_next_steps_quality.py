"""Tests for follow-up / next_steps normalization and filtering."""
from __future__ import annotations

from app.communication.followup_next_steps_quality import (
    filter_next_steps_and_questions,
    normalize_followup_line_item,
    normalize_followup_line_list,
)


def test_normalize_string_uses_default_clickable():
    a = normalize_followup_line_item("  Hello ", default_clickable=False)
    assert a == {"text": "Hello", "clickable": False}
    b = normalize_followup_line_item("Hi", default_clickable=True)
    assert b == {"text": "Hi", "clickable": True}


def test_normalize_object_clickable_and_tap_to_send():
    assert normalize_followup_line_item(
        {"text": "A", "clickable": False}, default_clickable=True
    ) == {"text": "A", "clickable": False}
    assert normalize_followup_line_item(
        {"label": "B", "tap_to_send": True}, default_clickable=False
    ) == {"text": "B", "clickable": True}


def test_normalize_followup_line_list():
    raw = ["plain", {"text": "x", "clickable": True}]
    out = normalize_followup_line_list(raw, default_clickable=False)
    assert out[0]["clickable"] is False
    assert out[1]["clickable"] is True


def test_filter_preserves_clickable():
    steps = [{"text": "Call the plan", "clickable": False}]
    qs = [{"text": "What is PA?", "clickable": True}]
    os, oq = filter_next_steps_and_questions(
        steps,
        qs,
        response_sources=[],
        answer_card=None,
    )
    assert os == steps
    assert oq == qs
