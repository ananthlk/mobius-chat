"""Unit tests for doc assembly pipeline: confidence labels, filter_abstain, best_score,
apply_google_fallback, assemble_with_neighbors, assemble_docs.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.services.doc_assembly import (
    DocAssemblyConfig,
    assign_confidence,
    assign_confidence_batch,
    filter_abstain,
    best_score,
    apply_google_fallback,
    assemble_with_neighbors,
    assemble_docs,
    google_search_via_skills_api,
)


# --- assign_confidence ---

def test_assign_confidence_abstain():
    """Score < 0.5 → abstain."""
    doc = {"text": "x", "rerank_score": 0.3}
    out = assign_confidence(doc)
    assert out["confidence_label"] == "abstain"
    assert out["llm_guidance"] == "Do not send"
    assert out["rerank_score"] == 0.3


def test_assign_confidence_process_with_caution():
    """Score 0.5–0.85 → process_with_caution."""
    doc = {"text": "x", "rerank_score": 0.7}
    out = assign_confidence(doc)
    assert out["confidence_label"] == "process_with_caution"
    assert out["llm_guidance"] == "Use but reconcile across docs"
    assert out["rerank_score"] == 0.7


def test_assign_confidence_process_confident():
    """Score >= 0.85 → process_confident."""
    doc = {"text": "x", "rerank_score": 0.9}
    out = assign_confidence(doc)
    assert out["confidence_label"] == "process_confident"
    assert out["llm_guidance"] == "Likely correct; verify no conflicts"
    assert out["rerank_score"] == 0.9


def test_assign_confidence_fallback_match_score():
    """Uses match_score when rerank_score absent."""
    doc = {"text": "x", "match_score": 0.75}
    out = assign_confidence(doc)
    assert out["rerank_score"] == 0.75
    assert out["confidence_label"] == "process_with_caution"


def test_assign_confidence_fallback_confidence():
    """Uses confidence when rerank_score and match_score absent."""
    doc = {"text": "x", "confidence": 0.92}
    out = assign_confidence(doc)
    assert out["rerank_score"] == 0.92
    assert out["confidence_label"] == "process_confident"


def test_assign_confidence_invalid_score_defaults_to_zero():
    """Invalid score → 0.0 → abstain."""
    doc = {"text": "x", "rerank_score": "not-a-number"}
    out = assign_confidence(doc)
    assert out["rerank_score"] == 0.0
    assert out["confidence_label"] == "abstain"


def test_assign_confidence_custom_config():
    """Custom config thresholds."""
    cfg = DocAssemblyConfig(confidence_abstain_max=0.4, confidence_process_confident_min=0.9)
    doc = {"text": "x", "rerank_score": 0.5}
    out = assign_confidence(doc, config=cfg)
    assert out["confidence_label"] == "process_with_caution"
    doc2 = {"text": "x", "rerank_score": 0.88}
    out2 = assign_confidence(doc2, config=cfg)
    assert out2["confidence_label"] == "process_with_caution"
    doc3 = {"text": "x", "rerank_score": 0.91}
    out3 = assign_confidence(doc3, config=cfg)
    assert out3["confidence_label"] == "process_confident"


# --- assign_confidence_batch ---

def test_assign_confidence_batch():
    """Batch assigns confidence to all chunks."""
    chunks = [
        {"text": "a", "rerank_score": 0.2},
        {"text": "b", "rerank_score": 0.9},
    ]
    out = assign_confidence_batch(chunks)
    assert len(out) == 2
    assert out[0]["confidence_label"] == "abstain"
    assert out[1]["confidence_label"] == "process_confident"


# --- filter_abstain ---

def test_filter_abstain_removes_abstain():
    """filter_abstain removes chunks with confidence_label abstain."""
    chunks = [
        {"text": "a", "confidence_label": "abstain"},
        {"text": "b", "confidence_label": "process_with_caution"},
        {"text": "c", "confidence_label": "process_confident"},
    ]
    out = filter_abstain(chunks)
    assert len(out) == 2
    assert [c["text"] for c in out] == ["b", "c"]


def test_filter_abstain_empty():
    """filter_abstain on empty list."""
    assert filter_abstain([]) == []


# --- best_score ---

def test_best_score_empty():
    """Empty list → 0."""
    assert best_score([]) == 0.0


def test_best_score_rerank():
    """Uses rerank_score."""
    chunks = [{"rerank_score": 0.8}, {"rerank_score": 0.5}]
    assert best_score(chunks) == 0.8


def test_best_score_fallback_match():
    """Falls back to match_score."""
    chunks = [{"match_score": 0.6}]
    assert best_score(chunks) == 0.6


# --- apply_google_fallback ---

def test_apply_google_fallback_high_confidence_corpus_only():
    """Best >= 0.85 → corpus only, no Google call."""
    chunks = [{"text": "a", "rerank_score": 0.9}]
    emitted = []
    out = apply_google_fallback(chunks, "q", emitter=emitted.append)
    assert len(out) == 1
    assert out[0]["text"] == "a"
    assert any("Corpus confidence sufficient" in s for s in emitted)


def test_apply_google_fallback_mid_confidence_no_google_url():
    """Best 0.5–0.85, no CHAT_SKILLS_GOOGLE_SEARCH_URL → corpus only."""
    chunks = [{"text": "a", "rerank_score": 0.7}]
    with patch.dict("os.environ", {}, clear=False):
        if "CHAT_SKILLS_GOOGLE_SEARCH_URL" in __import__("os").environ:
            del __import__("os").environ["CHAT_SKILLS_GOOGLE_SEARCH_URL"]
    emitted = []
    out = apply_google_fallback(chunks, "q", emitter=emitted.append)
    assert len(out) == 1
    assert any("Adding external search" in s for s in emitted)


def test_apply_google_fallback_low_confidence_no_google_url():
    """Best < 0.5, no Google URL → returns filter_abstain(corpus) (no external results)."""
    chunks = [{"text": "a", "rerank_score": 0.3}]
    with patch.dict("os.environ", {}, clear=False):
        pass  # ensure no CHAT_SKILLS_GOOGLE_SEARCH_URL
    emitted = []
    out = apply_google_fallback(chunks, "q", emitter=emitted.append)
    assert any("Low corpus confidence" in s for s in emitted)
    # All chunks are abstain, so filter_abstain removes them; no Google → empty
    assert len(out) == 0


def test_apply_google_fallback_empty_chunks():
    """Empty chunks, no Google URL → empty result."""
    with patch.dict("os.environ", {}, clear=False):
        out = apply_google_fallback([], "q")
    assert out == []


def test_apply_google_fallback_with_mock_google():
    """Best < 0.5 with mocked Google API → Google results returned."""
    chunks = [{"text": "a", "rerank_score": 0.3}]
    mock_google = [{
        "text": "Ext snippet",
        "source_type": "external",
        "confidence_label": "abstain",
        "llm_guidance": "External source; use if helpful but retain/hedge; not from authoritative corpus.",
        "rerank_score": 0.0,
    }]

    with patch("app.services.doc_assembly.google_search_via_skills_api", return_value=mock_google):
        out = apply_google_fallback(chunks, "test query")

    assert len(out) == 1
    assert out[0]["source_type"] == "external"
    assert out[0]["confidence_label"] == "abstain"
    assert "External source" in out[0]["llm_guidance"]


# --- assemble_with_neighbors ---

def test_assemble_with_neighbors_no_database_url():
    """No database_url → no expansion, returns copy of chunks."""
    chunks = [{"id": "1", "text": "a", "document_id": "d1", "paragraph_index": 0}]
    out = assemble_with_neighbors(chunks, "")  # empty database_url
    assert len(out) == 1
    assert out[0]["text"] == "a"


def test_assemble_with_neighbors_deduplicates():
    """Deduplicates by id."""
    chunks = [
        {"id": "1", "text": "a", "document_id": "d1", "paragraph_index": 0},
        {"id": "1", "text": "a2", "document_id": "d1", "paragraph_index": 0},
    ]
    out = assemble_with_neighbors(chunks, "postgres://none", window=2)
    assert len(out) == 1


# --- assemble_docs ---

def test_assemble_docs_no_google():
    """apply_google=False → confidence assigned, abstain filtered, no Google."""
    chunks = [
        {"text": "a", "rerank_score": 0.9},
        {"text": "b", "rerank_score": 0.3},
    ]
    out = assemble_docs(chunks, "q", apply_google=False)
    assert len(out) == 1
    assert out[0]["text"] == "a"
    assert out[0]["confidence_label"] == "process_confident"


def test_assemble_docs_with_google_high_confidence():
    """apply_google=True, best >= 0.85 → corpus only."""
    chunks = [{"text": "a", "rerank_score": 0.9}]
    out = assemble_docs(chunks, "q", apply_google=True)
    assert len(out) == 1
    assert out[0]["text"] == "a"


def test_assemble_docs_expand_neighbors_no_db():
    """expand_neighbors=True, database_url=None → no expansion (no DB)."""
    chunks = [{"id": "1", "text": "a", "rerank_score": 0.9, "document_id": "d1", "paragraph_index": 0}]
    out = assemble_docs(chunks, "q", expand_neighbors=True, database_url=None, apply_google=False)
    assert len(out) == 1


def test_google_search_via_skills_api_no_url():
    """No CHAT_SKILLS_GOOGLE_SEARCH_URL → returns []."""
    with patch.dict("os.environ", {}, clear=False):
        if "CHAT_SKILLS_GOOGLE_SEARCH_URL" in __import__("os").environ:
            del __import__("os").environ["CHAT_SKILLS_GOOGLE_SEARCH_URL"]
    out = google_search_via_skills_api("test")
    assert out == []


def test_google_search_via_skills_api_with_passed_base():
    """Pass api_base, mock urlopen → returns parsed results."""
    mock_response = json.dumps({
        "results": [
            {"snippet": "s1", "title": "t1", "url": "u1"},
        ],
    }).encode()
    with patch("urllib.request.urlopen") as mock_open:
        mock_open.return_value.read.return_value = mock_response
        mock_open.return_value.__enter__ = lambda self: self
        mock_open.return_value.__exit__ = lambda *a: None
        out = google_search_via_skills_api("q", api_base="https://example.com/search?")
    assert len(out) == 1
    assert out[0]["source_type"] == "external"
    assert "t1" in out[0]["text"] or "s1" in out[0]["text"]


# --- Integration: non_patient_rag → doc assembly ---

def test_non_patient_rag_sources_have_confidence_when_chunks_injected():
    """Integration: when retrieval returns chunks, doc assembly adds confidence_label to sources."""
    fake_chunks = [
        {"text": "Eligibility requires prior auth.", "rerank_score": 0.9, "document_name": "Doc1"},
    ]

    async def fake_gen(prompt):
        return ("Yes, prior auth is required.", {})

    mock_rag = type("RAG", (), {
        "vertex_index_endpoint_id": "ep",
        "vertex_deployed_index_id": "idx",
        "database_url": "postgres://localhost/test",
        "top_k": 10,
        "filter_payer": "",
        "filter_state": "",
        "filter_program": "",
        "filter_authority_level": "",
    })()
    mock_cfg_val = type("Cfg", (), {
        "rag": mock_rag,
        "prompts": type("P", (), {"rag_answering_user_template": "{context}\n\n{question}"})(),
    })()

    with (
        patch("app.chat_config.get_chat_config", return_value=mock_cfg_val),
        patch("app.services.retriever_backend.retrieve_for_chat", return_value=(fake_chunks, None)),
        patch("app.services.llm_provider.get_llm_provider") as mock_llm,
    ):
        from app.services.non_patient_rag import answer_non_patient
        mock_provider = type("Provider", (), {"generate_with_usage": lambda self, prompt: fake_gen(prompt)})()
        mock_llm.return_value = mock_provider
        full_msg, sources, usage, _ = answer_non_patient("Does eligibility require prior auth?")

    assert len(sources) == 1
    assert sources[0].get("confidence_label") == "process_confident"
    assert "llm_guidance" in sources[0]
    assert sources[0].get("rerank_score") == 0.9
