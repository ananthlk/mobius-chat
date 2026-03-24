"""follow-up / next_steps filtering and collapse defaults."""

from __future__ import annotations

from app.communication.followup_next_steps_quality import (
    filter_next_steps_and_questions,
    followup_blocks_collapsed_default,
    has_corpus_sources,
)


def test_filter_drops_upload_ask_when_corpus_and_no_required_vars():
    src = [{"index": 1, "document_id": "doc-1", "document_name": "Manual"}]
    card = {"mode": "FACTUAL", "direct_answer": "x", "sections": []}
    steps, qs = filter_next_steps_and_questions(
        ["Upload a document to the portal", "Call member services"],
        ["Can you upload a PDF of your contract?", "What is the timely filing limit?"],
        response_sources=src,
        answer_card=card,
    )
    assert "Upload a document" not in steps
    assert "Call member services" in steps
    assert not any("upload" in q.lower() for q in qs)
    assert any("timely filing" in q.lower() for q in qs)


def test_filter_keeps_upload_ask_when_required_variables_set():
    src = [{"document_id": "x"}]
    card = {
        "mode": "FACTUAL",
        "direct_answer": "x",
        "sections": [],
        "required_variables": ["denial letter"],
    }
    _steps, qs = filter_next_steps_and_questions(
        [],
        ["Can you upload the denial letter?"],
        response_sources=src,
        answer_card=card,
    )
    assert len(qs) == 1


def test_filter_keeps_when_no_corpus():
    steps, qs = filter_next_steps_and_questions(
        [],
        ["Do you have a document that shows the code list?"],
        response_sources=[],
        answer_card={"mode": "FACTUAL", "direct_answer": "x", "sections": []},
    )
    assert len(qs) == 1


def test_has_corpus_sources():
    assert has_corpus_sources([{"document_id": "a"}]) is True
    assert has_corpus_sources([{"url": "https://x.com"}]) is False


def test_followup_collapsed_default_by_badge():
    assert followup_blocks_collapsed_default("approved_authoritative") is False
    assert followup_blocks_collapsed_default("approved_informational") is False
    assert followup_blocks_collapsed_default("informational_only") is True
    assert followup_blocks_collapsed_default("proceed_with_caution") is True
    assert followup_blocks_collapsed_default("") is True
