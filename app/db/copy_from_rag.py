"""Copy actual docs from a source RAG DB into our cloned DB. Run: python -m app.db.copy_from_rag

Set in .env:
  RAG_SOURCE_DATABASE_URL  = source (e.g. Mobius RAG Postgres with many chunks)
  RAG_DATABASE_URL         = target (our cloned DB)

To copy from the real RAG database (with many chunks): set RAG_SOURCE_DATABASE_URL to that
DB URL and run this script directly. Do not use simulate_publish_to_env.sh for that — the
simulation script uses a local rag_source that is only seeded with 1 doc + 1 chunk per run.
"""
import logging
import os
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent.parent
_dotenv = _root / ".env"
if _dotenv.exists():
    import dotenv
    dotenv.load_dotenv(_dotenv)

logger = logging.getLogger(__name__)


def _vec_to_list(v):
    """Convert pgvector result (Vector, list, memoryview) to list of floats."""
    if v is None:
        return None
    if hasattr(v, "tolist"):
        return v.tolist()
    if hasattr(v, "__iter__") and not isinstance(v, (str, bytes)):
        return list(v)
    return None


def _mask_url(url: str) -> str:
    """Mask password in a postgres URL for logging."""
    if "://" not in url or "@" not in url:
        return url
    try:
        pre, rest = url.split("://", 1)
        if "@" in rest:
            user_part, host_part = rest.rsplit("@", 1)
            if ":" in user_part:
                user, _ = user_part.split(":", 1)
                user_part = user + ":****"
            return f"{pre}://{user_part}@{host_part}"
    except Exception:
        pass
    return url


def run_copy() -> bool:
    """Copy documents, chunks, chunk_embeddings from source RAG DB to our RAG_DATABASE_URL. Returns True on success."""
    source_url = (os.getenv("RAG_SOURCE_DATABASE_URL") or os.getenv("SOURCE_RAG_DATABASE_URL") or "").strip()
    target_url = (os.getenv("RAG_DATABASE_URL") or os.getenv("CHAT_RAG_DATABASE_URL") or "").strip()
    if not source_url:
        logger.error("RAG_SOURCE_DATABASE_URL (or SOURCE_RAG_DATABASE_URL) not set. Set it in .env.")
        return False
    if not target_url:
        logger.error("RAG_DATABASE_URL not set. Set it in .env.")
        return False

    try:
        import psycopg2
        from pgvector.psycopg2 import register_vector
        from pgvector import Vector
    except ImportError as e:
        logger.error("Install psycopg2-binary and pgvector: pip install psycopg2-binary pgvector - %s", e)
        return False

    source_conn = target_conn = None
    try:
        logger.info("Connecting to source: %s", _mask_url(source_url))
        logger.info("Connecting to target: %s", _mask_url(target_url))
        source_conn = psycopg2.connect(source_url)
        register_vector(source_conn)
        target_conn = psycopg2.connect(target_url)
        target_conn.autocommit = False  # must set before register_vector (set_session not allowed inside a transaction)
        register_vector(target_conn)

        sc = source_conn.cursor()
        tc = target_conn.cursor()

        # 1. Copy documents (same IDs so chunks can reference them). Source must have same schema.
        try:
            sc.execute("SELECT id, name, source_type, created_at, COALESCE(metadata::text, '{}') FROM documents ORDER BY created_at")
        except Exception as e:
            logger.error("Source DB must have table 'documents' with columns id, name, source_type, created_at, metadata. %s", e)
            return False
        docs = sc.fetchall()
        logger.info("Source: found %d document(s)", len(docs))
        for row in docs:
            logger.info("  - %s (id=%s, type=%s)", row[1] or "(no name)", row[0], row[2] or "document")
        for row in docs:
            tc.execute(
                """
                INSERT INTO documents (id, name, source_type, created_at, metadata)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, source_type = EXCLUDED.source_type,
                  created_at = EXCLUDED.created_at, metadata = EXCLUDED.metadata
                """,
                row,
            )
        logger.info("Copied %d document(s) to target.", len(docs))

        # 2. Copy chunks
        try:
            sc.execute(
                "SELECT id, document_id, text, page_number, start_offset, end_offset, created_at, COALESCE(metadata::text, '{}') FROM chunks ORDER BY document_id, page_number NULLS LAST, id"
            )
        except Exception as e:
            logger.error("Source DB must have table 'chunks' with columns id, document_id, text, page_number, start_offset, end_offset, created_at, metadata. %s", e)
            return False
        chunks = sc.fetchall()
        # Chunks per document (document_id is index 1)
        doc_chunk_count: dict = {}
        for row in chunks:
            did = str(row[1])
            doc_chunk_count[did] = doc_chunk_count.get(did, 0) + 1
        num_docs_with_chunks = len(doc_chunk_count)
        logger.info("Source: found %d chunk(s) across %d document(s)", len(chunks), num_docs_with_chunks)
        for row in chunks:
            tc.execute(
                """
                INSERT INTO chunks (id, document_id, text, page_number, start_offset, end_offset, created_at, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET document_id = EXCLUDED.document_id, text = EXCLUDED.text,
                  page_number = EXCLUDED.page_number, start_offset = EXCLUDED.start_offset,
                  end_offset = EXCLUDED.end_offset, created_at = EXCLUDED.created_at, metadata = EXCLUDED.metadata
                """,
                row,
            )
        logger.info("Copied %d chunk(s) to target.", len(chunks))

        # 3. Copy chunk_embeddings (vector column)
        try:
            sc.execute("SELECT id, chunk_id, embedding, model_id, created_at FROM chunk_embeddings")
        except Exception as e:
            logger.error("Source DB must have table 'chunk_embeddings' with columns id, chunk_id, embedding, model_id, created_at. %s", e)
            return False
        rows = sc.fetchall()
        valid_vectors = sum(1 for r in rows if _vec_to_list(r[2]) is not None)
        logger.info("Source: found %d chunk_embedding(s) (%d with valid vector)", len(rows), valid_vectors)
        if len(rows) != valid_vectors:
            logger.warning("Skipping %d chunk_embedding(s) with missing/invalid vector.", len(rows) - valid_vectors)
        for row in rows:
            emb = row[2]
            vec_list = _vec_to_list(emb) if emb is not None else None
            if vec_list is None:
                logger.warning("Skipping chunk_embedding id=%s (chunk_id=%s): no vector", row[0], row[1])
                continue
            tc.execute(
                """
                INSERT INTO chunk_embeddings (id, chunk_id, embedding, model_id, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (chunk_id) DO UPDATE SET embedding = EXCLUDED.embedding, model_id = EXCLUDED.model_id,
                  created_at = EXCLUDED.created_at
                """,
                (row[0], row[1], Vector(vec_list), row[3], row[4]),
            )
        logger.info("Copied %d chunk_embedding(s) to target.", valid_vectors)

        target_conn.commit()
        sc.close()
        tc.close()
        logger.info("Copy complete. Target now has: %d document(s), %d chunk(s), %d chunk_embedding(s). You can ask questions against the cloned DB.", len(docs), len(chunks), valid_vectors)
        return True
    except Exception as e:
        if target_conn:
            target_conn.rollback()
        logger.exception("Copy failed: %s", e)
        return False
    finally:
        if source_conn:
            source_conn.close()
        if target_conn:
            target_conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ok = run_copy()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
