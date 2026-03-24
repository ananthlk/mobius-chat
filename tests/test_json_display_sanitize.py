"""Tests for integrator direct_answer JSON bleed sanitization."""

import json

from app.communication.json_display_sanitize import (
    DEFAULT_BLEED_FALLBACK,
    build_minimal_answer_card_preserving_metadata,
    extract_user_visible_text_from_integrator_raw,
    finalize_answer_card_json_for_client,
    sanitize_direct_answer_string,
)


def test_sanitize_nested_direct_answer():
    inner = json.dumps(
        {
            "mode": "FACTUAL",
            "direct_answer": "The patient must enroll first.",
            "sections": [],
        }
    )
    out = sanitize_direct_answer_string(inner)
    assert out == "The patient must enroll first."
    assert "mode" not in out


def test_sanitize_resolutions_string():
    blob = json.dumps(
        {
            "resolutions": [
                {"sq_id": "sq1", "question": "Q", "resolution": "Plain answer.", "source": "rag"}
            ]
        }
    )
    assert sanitize_direct_answer_string(blob) == "Plain answer."


def test_finalize_replaces_bleed_with_fallback():
    card = {
        "mode": "FACTUAL",
        "direct_answer": '{"mode":"FACTUAL","direct_answer":"nested","sections":[]}',
        "sections": [],
    }
    msg = json.dumps(card)
    out = finalize_answer_card_json_for_client(msg, fallback_text=DEFAULT_BLEED_FALLBACK)
    parsed = json.loads(out)
    assert parsed["direct_answer"] == "nested"


def test_build_minimal_preserves_next_questions_and_sections():
    raw = """{
  "resolutions": [{"sq_id": "sq1", "question": "Q", "resolution": "R", "source": "rag"}],
  "next_questions_for_user": ["Do you have a plan type?"],
  "next_steps": ["Call member services"],
  "sections": [{"intent": "requirements", "label": "L", "bullets": ["b1"]}]
}"""
    visible = extract_user_visible_text_from_integrator_raw(raw)
    minimal = build_minimal_answer_card_preserving_metadata(visible, raw)
    assert minimal["direct_answer"] == visible
    assert minimal["next_questions_for_user"] == ["Do you have a plan type?"]
    assert minimal["next_steps"] == ["Call member services"]
    assert len(minimal["sections"]) == 1
    assert minimal["sections"][0].get("label") == "L"
    assert len(minimal["resolutions"]) == 1


def test_extract_resolutions_only_integrator_raw():
    """Invalid AnswerCard (no mode/sections) must still yield prose, not streamed JSON."""
    raw = """{
  "resolutions": [
    {
      "sq_id": "react_main",
      "question": "https://example.com/prior-auth",
      "resolution": "The link returned 404.",
      "source": "rag"
    }
  ]
}"""
    out = extract_user_visible_text_from_integrator_raw(raw)
    assert "resolutions" not in out
    assert "404" in out
    assert not out.strip().startswith("{")


def test_finalize_fallback_when_unextractable():
    card = {
        "mode": "FACTUAL",
        "direct_answer": '{"foo": 1, "bar": 2}',
        "sections": [],
    }
    msg = json.dumps(card)
    out = finalize_answer_card_json_for_client(msg, fallback_text="FALLBACK")
    parsed = json.loads(out)
    assert parsed["direct_answer"] == "FALLBACK"
