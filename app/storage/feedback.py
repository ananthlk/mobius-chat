"""Persist chat feedback (thumbs up/down + optional comment) in PostgreSQL.
Uses CHAT_RAG_DATABASE_URL (same DB as published_rag_metadata)."""
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _get_db_url() -> str:
    from app.chat_config import get_chat_config
    return (get_chat_config().rag.database_url or "").strip()


def insert_feedback(correlation_id: str, rating: str, comment: str | None) -> None:
    """Upsert one feedback row per correlation_id. rating must be 'up' or 'down'."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    url = _get_db_url()
    if not url:
        logger.warning("CHAT_RAG_DATABASE_URL not set; feedback not persisted")
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chat_feedback (correlation_id, rating, comment, created_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (correlation_id) DO UPDATE SET
                rating = EXCLUDED.rating,
                comment = EXCLUDED.comment,
                created_at = now()
            """,
            (correlation_id, rating, (comment or "").strip() or None),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Failed to persist feedback: %s", e)
        raise


def get_feedback(correlation_id: str) -> dict[str, Any] | None:
    """Return { rating, comment } or None if no feedback for this correlation_id."""
    url = _get_db_url()
    if not url:
        return None
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT rating, comment FROM chat_feedback WHERE correlation_id = %s",
            (correlation_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        return {"rating": row["rating"], "comment": row["comment"]}
    except Exception as e:
        logger.warning("Failed to get feedback: %s", e)
        return None


def insert_source_feedback(correlation_id: str, source_index: int, rating: str) -> None:
    """Upsert one source feedback row. source_index is 1-based. rating must be 'up' or 'down'."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    if source_index < 1:
        raise ValueError("source_index must be >= 1")
    url = _get_db_url()
    if not url:
        logger.warning("CHAT_RAG_DATABASE_URL not set; source feedback not persisted")
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chat_source_feedback (correlation_id, source_index, rating, created_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (correlation_id, source_index) DO UPDATE SET
                rating = EXCLUDED.rating,
                created_at = now()
            """,
            (correlation_id, source_index, rating),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Failed to persist source feedback: %s", e)
        raise


def get_source_feedback(correlation_id: str) -> list[dict[str, Any]]:
    """Return list of { source_index, rating } for this turn. Empty if none."""
    url = _get_db_url()
    if not url:
        return []
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT source_index, rating FROM chat_source_feedback WHERE correlation_id = %s ORDER BY source_index",
            (correlation_id,),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [{"source_index": int(r["source_index"]), "rating": r["rating"]} for r in rows]
    except Exception as e:
        logger.warning("Failed to get source feedback: %s", e)
        return []
