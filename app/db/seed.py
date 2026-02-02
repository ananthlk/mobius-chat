"""Seed RAG DB with one document + multiple chunks + embeddings so RAG returns results. Run: python -m app.db.seed"""
import logging
import os
import sys
from pathlib import Path

# Load .env from project root
_root = Path(__file__).resolve().parent.parent.parent
_dotenv = _root / ".env"
if _dotenv.exists():
    import dotenv
    dotenv.load_dotenv(_dotenv)

logger = logging.getLogger(__name__)

# Multiple sample chunks (policy-style) so one seed run adds many chunks
SAMPLE_CHUNKS = [
    "Molina Healthcare covers Medicaid and Medicare members in several states including Florida, California, and Texas. Eligibility depends on income and household size. To check if you qualify, contact your state Medicaid office or visit Molina's member portal.",
    "Prior authorization may be required for certain medications and procedures. Submit requests through the member portal or by fax. Standard turnaround is 14 calendar days for non-urgent requests.",
    "Appeal rights: If a claim is denied, you have 180 days from the date of the denial letter to file an appeal. Include your member ID, the denial letter, and any supporting documentation.",
    "Preventive care such as annual wellness visits and screenings are covered at no cost when using in-network providers. Out-of-network care may require higher cost-sharing.",
    "Prescription drug coverage is included in most plans. Tier 1 generics have the lowest copay; specialty drugs may require prior authorization. Check the formulary on the member portal.",
    "Behavioral health services including mental health and substance use treatment are covered. You may self-refer for a set number of outpatient visits per year; additional visits may require referral.",
    "Durable medical equipment (DME) such as wheelchairs and CPAP machines requires a prescription and may need prior authorization. Rentals and purchases are subject to plan limits.",
    "Emergency care is covered at in-network cost-sharing even when received out of network. If you believe you had an emergency, you can request that the plan review the claim.",
]


def run_seed() -> bool:
    """Insert one document, multiple chunks, and their embeddings. Returns True on success."""
    database_url = (os.getenv("RAG_DATABASE_URL") or os.getenv("CHAT_RAG_DATABASE_URL") or "").strip()
    if not database_url:
        logger.error("RAG_DATABASE_URL (or CHAT_RAG_DATABASE_URL) not set. Set it in .env and run again.")
        return False
    try:
        import psycopg2
        from pgvector.psycopg2 import register_vector
        from pgvector import Vector
    except ImportError as e:
        logger.error("Install psycopg2-binary and pgvector: pip install psycopg2-binary pgvector - %s", e)
        return False
    try:
        from app.services.embedding_provider import get_query_embedding
    except Exception as e:
        logger.error("Embedding provider failed (need Vertex credentials in .env): %s", e)
        return False

    conn = None
    try:
        conn = psycopg2.connect(database_url)
        conn.autocommit = False
        register_vector(conn)
        cur = conn.cursor()

        # One document
        cur.execute(
            "INSERT INTO documents (id, name, source_type) VALUES (gen_random_uuid(), %s, %s) RETURNING id",
            ("Sample Policy Doc", "document"),
        )
        doc_id = cur.fetchone()[0]

        # Multiple chunks + embeddings
        for i, text in enumerate(SAMPLE_CHUNKS, start=1):
            cur.execute(
                "INSERT INTO chunks (id, document_id, text, page_number) VALUES (gen_random_uuid(), %s, %s, %s) RETURNING id",
                (doc_id, text, i),
            )
            chunk_id = cur.fetchone()[0]
            embedding = get_query_embedding(text)
            cur.execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedding, model_id) VALUES (%s, %s, %s) ON CONFLICT (chunk_id) DO NOTHING",
                (chunk_id, Vector(embedding), "text-embedding-005"),
            )

        conn.commit()
        cur.close()
        logger.info("Seeded RAG DB: 1 document, %d chunks, %d embeddings. You can ask questions now.", len(SAMPLE_CHUNKS), len(SAMPLE_CHUNKS))
        return True
    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception("Seed failed: %s", e)
        return False
    finally:
        if conn:
            conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ok = run_seed()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
