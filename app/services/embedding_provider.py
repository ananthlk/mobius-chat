"""Embedding provider for RAG (Vertex AI). 1536 dims to match published RAG mart and Vertex index.

Uses same model as Mobius RAG: gemini-embedding-001 with output_dimensionality=1536.
"""
import logging
from typing import List

logger = logging.getLogger(__name__)

# Same as Mobius RAG (app/config.py + app/services/embedding_provider.py): Vertex uses gemini-embedding-001, 1536 dims
DEFAULT_EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMENSIONS = 1536


def get_query_embedding(text: str) -> List[float]:
    """Return 1536-dim embedding vector for one query string (Vertex AI). Same model as Mobius RAG published mart."""
    import os
    from app.chat_config import get_chat_config
    cfg = get_chat_config()
    # Never raise: always resolve to env or default (same as llm_provider)
    project_id = (cfg.llm.vertex_project_id or os.getenv("VERTEX_PROJECT_ID") or os.getenv("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new").strip() or "mobiusos-new"
    location = cfg.llm.vertex_location or "us-central1"
    try:
        import vertexai
        from vertexai.language_models import TextEmbeddingModel, TextEmbeddingInput
        vertexai.init(project=project_id, location=location)
        model = TextEmbeddingModel.from_pretrained(DEFAULT_EMBED_MODEL)
        # Same API as Mobius RAG: TextEmbeddingInput + output_dimensionality for gemini-embedding-001
        # Same task_type as Mobius RAG (index built with RETRIEVAL_DOCUMENT; query same for compatibility)
        inputs = [TextEmbeddingInput(text, task_type="RETRIEVAL_DOCUMENT")]
        resp = model.get_embeddings(inputs, output_dimensionality=EMBED_DIMENSIONS)
        if not resp or not resp[0].values:
            raise ValueError("Empty embedding returned")
        return list(resp[0].values)
    except Exception as e:
        logger.exception("Embedding failed: %s", e)
        raise
