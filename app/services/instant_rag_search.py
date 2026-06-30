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

    Calls RAG with document_id scoped pgvector search — no Chroma.
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
    except Exception as exc:
        logger.warning("instant-rag: RAG /api/query failed for doc=%s: %s", document_id[:8], exc)
        return ("", [], None, _SIGNAL_NO_SOURCES)

    chunks = data.get("chunks") or []
    if not chunks:
        logger.info("instant-rag: 0 chunks from RAG for doc=%s", document_id[:8])
        return ("", [], None, _SIGNAL_NO_SOURCES)

    sources: list[dict[str, Any]] = []
    for chunk in chunks:
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

    answer_text = "\n\n".join(s["text"] for s in sources if s["text"])

    _emit(
        emitter,
        f"  ✓ found {len(sources)} passage{'s' if len(sources) != 1 else ''} in your attached document.",
    )

    return (answer_text, sources, None, _SIGNAL_CORPUS_ONLY)


def _emit(emitter: Callable[[str], None] | None, line: str) -> None:
    if emitter and line.strip():
        try:
            emitter(line.strip())
        except Exception:
            pass
