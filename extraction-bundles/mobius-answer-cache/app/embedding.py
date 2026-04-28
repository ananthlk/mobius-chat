"""Vertex embedding wrapper.

Cache reuses the same model as rag (gemini-embedding-001, 1536-dim)
so the vector spaces align. Without this, a question embedded by
chat for retrieval and the same question embedded by cache for
lookup would land in different spaces, and cosine similarity
between them would be meaningless.

Phase 0: this module wraps Vertex's text-embeddings API directly.
Phase 1+ alternative (open question §6.2 in docs/SPEC.md): call
mobius-rag's embedding endpoint instead, so there's exactly one
embedding code path across services.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_MODEL = os.environ.get("EMBEDDING_MODEL") or "gemini-embedding-001"
_PROJECT = os.environ.get("VERTEX_PROJECT_ID") or ""
_LOCATION = os.environ.get("VERTEX_LOCATION") or "us-central1"


def embed_question(text: str) -> list[float]:
    """Synchronous embed. Raises on failure; caller wraps for tolerant
    behavior (cache_lookup returns a clean no-rows response on embed
    failure rather than 500ing).
    """
    if not _PROJECT:
        raise RuntimeError("VERTEX_PROJECT_ID not set")
    from vertexai.language_models import TextEmbeddingModel

    import vertexai
    vertexai.init(project=_PROJECT, location=_LOCATION)

    model = TextEmbeddingModel.from_pretrained(_MODEL)
    embeddings = model.get_embeddings([text[:8000]])
    if not embeddings:
        raise RuntimeError("vertex returned no embeddings")
    return list(embeddings[0].values)
