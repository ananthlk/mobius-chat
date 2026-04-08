#!/usr/bin/env python3
"""Seed ChromaDB from published_rag_metadata (Postgres).

Reads all chunks from published_rag_metadata, embeds via Vertex AI (gemini-embedding-001),
and upserts into a local ChromaDB collection with metadata for filtering.

Usage:
    # Set env vars (or use .env)
    export CHAT_RAG_DATABASE_URL="postgresql://user:pass@host:port/mobius_chat"
    export CHROMA_PERSIST_DIR="/Users/ananth/mobius-chroma"
    export VERTEX_PROJECT_ID="mobius-os-dev"

    python scripts/seed_chroma.py [--batch-size 50] [--limit 0] [--collection published_rag]

Requires: chromadb, psycopg2, vertexai (google-cloud-aiplatform)
"""
import argparse
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Add parent dir so `from app.services...` works when run from mobius-chat/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _load_env():
    """Load .env if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path)
            logger.info("Loaded .env from %s", env_path)
    except ImportError:
        pass


def _fetch_all_rows(database_url: str, limit: int = 0) -> list[dict]:
    """Fetch rows from published_rag_metadata. Returns list of dicts."""
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(database_url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    sql = """
        SELECT id, document_id, source_type, text,
               page_number, paragraph_index,
               document_payer, document_state, document_program,
               document_authority_level, document_display_name, document_filename
        FROM published_rag_metadata
        WHERE text IS NOT NULL AND text != ''
        ORDER BY id
    """
    if limit > 0:
        sql += f" LIMIT {limit}"
    cur.execute(sql)
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def _embed_batch(texts: list[str], project_id: str, location: str = "us-central1") -> list[list[float]]:
    """Embed a batch of texts via Vertex AI gemini-embedding-001 (1536 dims)."""
    import vertexai
    from vertexai.language_models import TextEmbeddingModel, TextEmbeddingInput

    vertexai.init(project=project_id, location=location)
    model = TextEmbeddingModel.from_pretrained("gemini-embedding-001")
    inputs = [TextEmbeddingInput(t, task_type="RETRIEVAL_DOCUMENT") for t in texts]
    resp = model.get_embeddings(inputs, output_dimensionality=1536)
    return [list(e.values) for e in resp]


def _deduplicate_rows(rows: list[dict]) -> list[dict]:
    """Deduplicate rows by text content (keep first occurrence per unique text).
    Postgres may have multiple rows with the same text from overlapping chunking strategies."""
    seen_texts: set[str] = set()
    unique: list[dict] = []
    for r in rows:
        text = (r.get("text") or "").strip()
        if text and text not in seen_texts:
            seen_texts.add(text)
            unique.append(r)
    return unique


def main():
    parser = argparse.ArgumentParser(description="Seed ChromaDB from published_rag_metadata")
    parser.add_argument("--batch-size", type=int, default=50, help="Embedding batch size (default 50)")
    parser.add_argument("--limit", type=int, default=0, help="Limit rows to process (0 = all)")
    parser.add_argument("--collection", type=str, default="published_rag", help="ChromaDB collection name")
    parser.add_argument("--no-dedup", action="store_true", help="Skip deduplication (keep all rows even if text is identical)")
    args = parser.parse_args()

    _load_env()

    database_url = os.getenv("CHAT_RAG_DATABASE_URL") or os.getenv("RAG_DATABASE_URL") or os.getenv("CHAT_DATABASE_URL")
    chroma_dir = os.getenv("CHROMA_PERSIST_DIR") or "/Users/ananth/mobius-chroma"
    project_id = os.getenv("VERTEX_PROJECT_ID") or os.getenv("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new"
    location = os.getenv("VERTEX_LOCATION") or "us-central1"

    if not database_url:
        logger.error("Set CHAT_RAG_DATABASE_URL (Postgres URL for published_rag_metadata)")
        sys.exit(1)

    logger.info("Database: %s", database_url.split("@")[-1] if "@" in database_url else "(hidden)")
    logger.info("ChromaDB dir: %s", chroma_dir)
    logger.info("Vertex project: %s / %s", project_id, location)
    logger.info("Batch size: %d, Limit: %s", args.batch_size, args.limit or "all")

    # Fetch rows
    logger.info("Fetching rows from published_rag_metadata...")
    rows = _fetch_all_rows(database_url, limit=args.limit)
    logger.info("Fetched %d rows with text", len(rows))
    if not rows:
        logger.warning("No rows found. Check database_url and that published_rag_metadata has data.")
        return

    # Deduplicate by text content (Postgres often has duplicate texts from overlapping chunking)
    if not args.no_dedup:
        before = len(rows)
        rows = _deduplicate_rows(rows)
        logger.info("Deduplicated: %d → %d unique texts (%d duplicates removed)", before, len(rows), before - len(rows))

    # Init ChromaDB
    import chromadb
    client = chromadb.PersistentClient(path=chroma_dir)
    coll = client.get_or_create_collection(
        name=args.collection,
        metadata={"hnsw:space": "cosine"},
    )
    logger.info("ChromaDB collection '%s' — %d existing items", args.collection, coll.count())

    # Process in batches
    total = len(rows)
    processed = 0
    skipped = 0
    t0 = time.time()

    for batch_start in range(0, total, args.batch_size):
        batch = rows[batch_start:batch_start + args.batch_size]
        texts = [r["text"] for r in batch]
        ids = [str(r["id"]) for r in batch]

        # Embed
        try:
            embeddings = _embed_batch(texts, project_id, location)
        except Exception as e:
            logger.error("Embedding batch %d-%d failed: %s", batch_start, batch_start + len(batch), e)
            skipped += len(batch)
            continue

        # Build metadata (Chroma requires str/int/float/bool values)
        metadatas = []
        for r in batch:
            metadatas.append({
                "document_id": str(r["document_id"]) if r.get("document_id") else "",
                "source_type": str(r.get("source_type") or ""),
                "document_payer": str(r.get("document_payer") or ""),
                "document_state": str(r.get("document_state") or ""),
                "document_program": str(r.get("document_program") or ""),
                "document_authority_level": str(r.get("document_authority_level") or ""),
            })

        # Upsert into ChromaDB
        try:
            coll.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)
            processed += len(batch)
        except Exception as e:
            logger.error("ChromaDB upsert batch %d-%d failed: %s", batch_start, batch_start + len(batch), e)
            skipped += len(batch)
            continue

        elapsed = time.time() - t0
        rate = processed / elapsed if elapsed > 0 else 0
        logger.info(
            "Progress: %d/%d (%.0f%%) — %.1f rows/sec — skipped %d",
            processed, total, 100 * processed / total, rate, skipped,
        )

    elapsed = time.time() - t0
    logger.info("Done. %d processed, %d skipped in %.1fs", processed, skipped, elapsed)
    logger.info("ChromaDB collection '%s' now has %d items at %s", args.collection, coll.count(), chroma_dir)


if __name__ == "__main__":
    main()
