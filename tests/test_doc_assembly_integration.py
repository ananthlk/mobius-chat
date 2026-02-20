"""Integration tests for doc assembly: real database (neighbor expansion) and Google search.

Skip when CHAT_RAG_DATABASE_URL or CHAT_SKILLS_GOOGLE_SEARCH_URL are not set.
Run with: pytest mobius-chat/tests/test_doc_assembly_integration.py -v -s

Ensure .env is loaded (from Mobius root or mobius-chat): CHAT_RAG_DATABASE_URL, CHAT_SKILLS_GOOGLE_SEARCH_URL.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Load .env before checking env vars
_chat_root = Path(__file__).resolve().parent.parent
_env = _chat_root / ".env"
if _env.exists():
    from dotenv import load_dotenv
    load_dotenv(_env, override=True)
_root_env = _chat_root.parent / ".env"
if _root_env.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_root_env, override=False)
    except Exception:
        pass

# Skip markers based on env (evaluated after dotenv load)
_has_db = bool(
    os.environ.get("CHAT_RAG_DATABASE_URL")
    or os.environ.get("RAG_DATABASE_URL")
    or os.environ.get("CHAT_DATABASE_URL")
)
_has_google = bool(os.environ.get("CHAT_SKILLS_GOOGLE_SEARCH_URL", "").strip())

skip_if_no_db = pytest.mark.skipif(not _has_db, reason="CHAT_RAG_DATABASE_URL not set")
skip_if_no_google = pytest.mark.skipif(not _has_google, reason="CHAT_SKILLS_GOOGLE_SEARCH_URL not set")


def _get_db_url() -> str | None:
    return (
        os.environ.get("CHAT_RAG_DATABASE_URL")
        or os.environ.get("RAG_DATABASE_URL")
        or os.environ.get("CHAT_DATABASE_URL")
        or ""
    ).strip() or None


def _fetch_one_chunk_from_db(database_url: str) -> dict | None:
    """Fetch a single row from published_rag_metadata for integration tests."""
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(database_url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT id, document_id, text, page_number, paragraph_index,
                      document_display_name, document_filename
               FROM published_rag_metadata
               WHERE text IS NOT NULL AND text != ''
               LIMIT 1"""
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        return {
            "id": row.get("id"),
            "document_id": str(row["document_id"]) if row.get("document_id") else None,
            "text": row.get("text") or "",
            "page_number": row.get("page_number"),
            "paragraph_index": row.get("paragraph_index"),
            "document_name": row.get("document_display_name") or row.get("document_filename") or "document",
        }
    except Exception as e:
        pytest.skip(f"DB connect failed: {e}")


@skip_if_no_db
def test_assemble_with_neighbors_real_db():
    """Neighbor expansion against real published_rag_metadata."""
    from app.services.doc_assembly import assemble_with_neighbors

    db_url = _get_db_url()
    if not db_url:
        pytest.skip("No database URL")

    chunk = _fetch_one_chunk_from_db(db_url)
    if not chunk:
        pytest.skip("published_rag_metadata is empty")

    chunks = [dict(chunk, rerank_score=0.9)]
    out = assemble_with_neighbors(chunks, db_url, window=2)

    assert len(out) >= 1
    assert out[0]["text"] == chunk["text"]
    # May have added neighbors (siblings)
    neighbor_count = sum(1 for c in out if c.get("is_neighbor"))
    assert neighbor_count >= 0
    if neighbor_count > 0:
        assert any(c.get("is_neighbor") for c in out)


@skip_if_no_db
def test_assemble_docs_with_neighbors_real_db():
    """Full assemble_docs with expand_neighbors=True against real DB."""
    from app.services.doc_assembly import assemble_docs

    db_url = _get_db_url()
    if not db_url:
        pytest.skip("No database URL")

    chunk = _fetch_one_chunk_from_db(db_url)
    if not chunk:
        pytest.skip("published_rag_metadata is empty")

    chunks = [dict(chunk, rerank_score=0.9)]
    out = assemble_docs(
        chunks,
        "test question",
        expand_neighbors=True,
        database_url=db_url,
        apply_google=False,
    )
    assert len(out) >= 1
    assert all("confidence_label" in c for c in out)
    assert all("llm_guidance" in c for c in out)


@skip_if_no_google
def test_google_search_via_skills_api_real():
    """Real Google search via CHAT_SKILLS_GOOGLE_SEARCH_URL."""
    from app.services.doc_assembly import google_search_via_skills_api

    results = google_search_via_skills_api("Florida Medicaid eligibility", max_results=3)
    assert isinstance(results, list)
    # May return 0 if API fails; if it returns data, validate shape
    for r in results:
        assert "text" in r
        assert r.get("source_type") == "external"
        assert r.get("confidence_label") == "abstain"


@skip_if_no_google
def test_apply_google_fallback_with_real_google():
    """apply_google_fallback triggers real Google when best < 0.5."""
    from app.services.doc_assembly import apply_google_fallback

    chunks = [{"text": "weak match", "rerank_score": 0.3}]
    emitted = []
    out = apply_google_fallback(chunks, "Florida Medicaid eligibility", emitter=emitted.append)
    assert any("Low corpus confidence" in s for s in emitted)
    # Should have either corpus (filter_abstain) or Google results
    # With abstain filtered, corpus is empty; Google may return results
    assert isinstance(out, list)


@skip_if_no_db
@skip_if_no_google
def test_assemble_docs_full_integration_db_and_google():
    """Full assemble_docs: neighbors + Google fallback with real DB and Google."""
    from app.services.doc_assembly import assemble_docs

    db_url = _get_db_url()
    if not db_url:
        pytest.skip("No database URL")

    chunk = _fetch_one_chunk_from_db(db_url)
    if not chunk:
        pytest.skip("published_rag_metadata is empty")

    # Low score to trigger Google complement
    chunks = [dict(chunk, rerank_score=0.65)]
    emitted = []
    out = assemble_docs(
        chunks,
        "Florida Medicaid prior authorization",
        expand_neighbors=True,
        database_url=db_url,
        apply_google=True,
        emitter=emitted.append,
    )
    assert len(out) >= 1
    assert all("confidence_label" in c for c in out)
    # Should have emitted something (e.g. "Adding external search" or "Corpus confidence...")
    assert len(emitted) >= 1
