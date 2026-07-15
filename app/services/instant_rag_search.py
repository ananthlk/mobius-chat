"""Instant-RAG lazy search (Phase B.1).

Dedicated retrieval path for user-uploaded documents.  Calls the RAG
service's /api/query endpoint with a document_id filter so the search
runs entirely in pgvector — no Chroma dependency.

Why pgvector instead of Chroma
-------------------------------
The RAG pipeline writes embeddings to ``chunk_embeddings`` (pgvector) and
promotes them to ``rag_published_embeddings`` (also pgvector).  A shared
Chroma server is no longer part of the production stack; it was removed
after the 2026-04-27 pgvector migration.  Instant-rag chat uploads were
never visible in Chroma anyway because ``VECTOR_STORE=pgvector`` gates off
the Chroma upsert in ``publish_sync.py``.

The RAG /api/query endpoint now accepts an optional ``document_id`` query
param that scopes the pgvector ANN search to a single document, making it
the natural replacement for the old Chroma ``thread_corpus_search`` path.

Deterministic fallback
-----------------------
ANN search can return 0 results even when the document is indexed — e.g.
when the query is too generic ("what does this file say?") relative to a
tiny private doc, or when the embedding distance exceeds the vector store's
effective threshold.  When ANN returns nothing, we fall back to
``/documents/{id}/pages`` which fetches raw page text directly from the
extraction store — no similarity scoring, always deterministic.  A user who
explicitly uploaded a doc and is asking about it must receive that doc's
content regardless of phrasing.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable

logger = logging.getLogger(__name__)

_SIGNAL_NO_SOURCES = "no_sources"
_SIGNAL_CORPUS_ONLY = "corpus_only"


def lazy_rag_search(
    document_id: str,
    question: str,
    k: int = 8,
    *,
    emitter: Callable[[str], None] | None = None,
) -> tuple[str, list[dict[str, Any]], dict | None, str]:
    """Vector-search a single uploaded document via RAG /api/query.

    Primary: scoped pgvector ANN search (document_id filter).
    Fallback: deterministic page-text fetch via /documents/{id}/pages when
    ANN returns 0 results — covers generic queries against tiny private docs.

    Returns the same (answer_text, sources, usage, signal) tuple shape
    as the old Chroma path so callers (react_loop fan-out) are unchanged.
    """
    if not document_id or not document_id.strip():
        return ("", [], None, _SIGNAL_NO_SOURCES)
    if not question or not question.strip():
        return ("", [], None, _SIGNAL_NO_SOURCES)

    rag_url = (os.environ.get("MOBIUS_RAG_URL") or os.environ.get("RAG_API_URL") or "").rstrip("/")
    if not rag_url:
        logger.warning("instant-rag: MOBIUS_RAG_URL not configured")
        return ("", [], None, _SIGNAL_NO_SOURCES)

    _emit(emitter, "Reading your attached document…")

    # ── Primary: ANN search scoped to this document_id ──────────────────
    sources: list[dict[str, Any]] = []
    try:
        body = json.dumps({"query": question, "k": k, "document_id": document_id}).encode()
        req = urllib.request.Request(
            f"{rag_url}/api/query",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        for chunk in (data.get("chunks") or []):
            sources.append({
                "id": chunk.get("source_id") or chunk.get("document_id"),
                "text": chunk.get("text") or "",
                "document_id": chunk.get("document_id") or document_id,
                "document_name": chunk.get("document_name"),
                "page_number": chunk.get("page_number"),
                "source_type": "instant_rag",
                "rerank_score": chunk.get("similarity"),
                "instant_rag": True,
            })
        if sources:
            logger.debug("instant-rag: ANN returned %d chunks for doc=%s", len(sources), document_id[:8])
    except Exception as exc:
        logger.warning("instant-rag: RAG /api/query failed for doc=%s: %s", document_id[:8], exc)

    # ── Deterministic fallback: fetch page text when ANN returns nothing ──
    if not sources:
        logger.info(
            "instant-rag: ANN 0 results for doc=%s — falling back to page-text fetch",
            document_id[:8],
        )
        sources = _fetch_pages_as_sources(rag_url, document_id)

    if not sources:
        logger.info("instant-rag: no content found for doc=%s (ANN + page-text both empty)", document_id[:8])
        return ("", [], None, _SIGNAL_NO_SOURCES)

    answer_text = "\n\n".join(s["text"] for s in sources if s["text"])

    _emit(
        emitter,
        f"  ✓ found {len(sources)} passage{'s' if len(sources) != 1 else ''} in your attached document.",
    )

    return (answer_text, sources, None, _SIGNAL_CORPUS_ONLY)


def _fetch_pages_as_sources(rag_url: str, document_id: str) -> list[dict[str, Any]]:
    """Deterministic fallback: fetch raw page text from /documents/{id}/pages.

    Returns a sources list in the same shape as the ANN path.  Page text is
    truncated to 4000 chars per page so a large multi-page doc doesn't flood
    the integrator's context window.
    """
    try:
        req = urllib.request.Request(
            f"{rag_url}/documents/{document_id}/pages",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.warning("instant-rag: /pages fallback failed for doc=%s: %s", document_id[:8], exc)
        return []

    pages = data.get("pages") or []
    sources: list[dict[str, Any]] = []
    for page in pages:
        text = (page.get("text") or "").strip()[:4000]
        if not text:
            continue
        sources.append({
            "id": document_id,
            "text": text,
            "document_id": document_id,
            "document_name": data.get("filename") or data.get("document_name"),
            "page_number": page.get("page_number"),
            "source_type": "instant_rag",
            "rerank_score": None,
            "instant_rag": True,
        })
    return sources


def _emit(emitter: Callable[[str], None] | None, line: str) -> None:
    if emitter and line.strip():
        try:
            emitter(line.strip())
        except Exception:
            pass
