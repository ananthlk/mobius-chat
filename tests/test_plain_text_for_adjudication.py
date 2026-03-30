"""plain_text_for_adjudication_from_chat_message: wire JSON → user-visible text for QA."""

from __future__ import annotations

import json

from app.communication.json_display_sanitize import plain_text_for_adjudication_from_chat_message


def test_answer_card_json_becomes_prose_not_braces() -> None:
    card = {
        "mode": "FACTUAL",
        "direct_answer": "Summary for the user.",
        "sections": [
            {"label": "Appeal steps", "bullets": ["Submit in writing", "Include member ID"]},
        ],
    }
    wire = json.dumps(card)
    out = plain_text_for_adjudication_from_chat_message(wire)
    assert "Summary for the user." in out
    assert "Appeal steps" in out
    assert "Submit in writing" in out
    assert not out.strip().startswith("{")


def test_empty_sections_uses_direct_answer_only() -> None:
    card = {
        "mode": "FACTUAL",
        "direct_answer": "Full instructions are all in this paragraph.",
        "sections": [],
    }
    out = plain_text_for_adjudication_from_chat_message(json.dumps(card))
    assert "Full instructions" in out
    assert not out.strip().startswith("{")


def test_non_json_unchanged() -> None:
    s = "Plain fallback message."
    assert plain_text_for_adjudication_from_chat_message(s) == s
