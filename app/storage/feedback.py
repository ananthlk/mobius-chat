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
