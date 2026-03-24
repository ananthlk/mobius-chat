"""AnswerCard strict parse: invalid section intents must not drop the whole card."""
from __future__ import annotations

from app.responder.final import _parse_answer_card


def test_parse_coerces_invalid_section_intent():
    raw = (
        '{"mode":"FACTUAL","direct_answer":"Short answer.",'
        '"sections":[{"label":"Support","intent":"summary","bullets":["a"]}]}'
    )
    out = _parse_answer_card(raw)
    assert out is not None
    assert out["sections"][0]["intent"] == "references"
    assert out["sections"][0]["label"] == "Support"


def test_parse_fills_missing_intent():
    raw = '{"mode":"FACTUAL","direct_answer":"Hi","sections":[{"label":"Refs","bullets":["x"]}]}'
    out = _parse_answer_card(raw)
    assert out is not None
    assert out["sections"][0]["intent"] == "references"


def test_parse_drops_non_dict_sections():
    raw = '{"mode":"FACTUAL","direct_answer":"Hi","sections":[1,2,{"label":"OK","intent":"definitions","bullets":["z"]}]}'
    out = _parse_answer_card(raw)
    assert out is not None
    assert len(out["sections"]) == 1
    assert out["sections"][0]["intent"] == "definitions"
