"""Persist chat feedback (thumbs up/down + optional comment) in PostgreSQL.
Uses CHAT_RAG_DATABASE_URL (same DB as published_rag_metadata).

Phase 0.17 — fail-closed in non-dev
-----------------------------------
Before 0.17, every ``insert_*`` fn silently returned when ``CHAT_RAG_DATABASE_URL``
was unset (logged WARNING, caller saw success). Two of them (``insert_adjudication_feedback``,
``insert_llm_performance_feedback``) also swallowed "relation does not
exist" at DEBUG level, so if migrations 024/025 never ran, user thumbs
vanished silently.

The fix keeps dev ergonomics while making prod honest:

- ``CHAT_ENV`` env var: ``dev`` (default) / ``staging`` / ``prod``.
- In ``dev``: missing URL or missing table still degrade to a log line
  and return, so local dev without Postgres keeps working.
- In ``staging`` / ``prod``: missing URL → ``FeedbackPersistenceError``
  at the storage layer; missing table → same. Callers get a real
  500 at the HTTP boundary instead of feedback silently vanishing.

The storage layer does NOT decide how to respond at the HTTP level —
it just fails loudly when the environment claims to be hosted. The
``app.api.feedback`` router keeps the same shape; the error surfaces
naturally via FastAPI's unhandled-exception → 500 path.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class FeedbackPersistenceError(RuntimeError):
    """Raised in non-dev when a feedback write can't reach its target.

    Storage-layer exception; the router catches nothing by default so
    FastAPI returns 500 to the caller. Dev callers never see this —
    the storage fn degrades to a log + return when ``CHAT_ENV=dev``.
    """


def _env_is_hosted() -> bool:
    """True when we're in staging or prod (missing persistence = hard error).

    Default ``dev`` keeps local workflows identical to the pre-0.17 behavior.
    Any value that isn't 'dev' or 'development' is treated as hosted — prefer
    to err on the side of loud failure.
    """
    env = (os.environ.get("CHAT_ENV") or "dev").strip().lower()
    return env not in ("dev", "development", "local")


def _get_db_url() -> str:
    from app.chat_config import get_chat_config
    return (get_chat_config().rag.database_url or "").strip()


def _handle_missing_db_url(kind: str) -> None:
    """Called from every insert_* when the URL is unset. Raises in hosted envs."""
    msg = f"CHAT_RAG_DATABASE_URL not set; {kind} cannot be persisted"
    if _env_is_hosted():
        logger.error("[fail-closed] %s (CHAT_ENV=%r)", msg, os.environ.get("CHAT_ENV"))
        raise FeedbackPersistenceError(msg)
    logger.warning(msg)


def _handle_missing_relation(kind: str, migration_num: str, e: Exception) -> None:
    """Called when Postgres reports 'relation does not exist'. Raises in hosted envs.

    ``migration_num`` is the chat DB migration that creates the table
    (024 for llm_performance_feedback, 025 for adjudication_feedback).
    Mentioning it in the error makes ops debugging obvious: "the migration
    didn't run on this deploy" is a clear next step.
    """
    msg = (
        f"{kind} table missing — run chat DB migration {migration_num}. "
        f"Underlying error: {e}"
    )
    if _env_is_hosted():
        logger.error("[fail-closed] %s", msg)
        raise FeedbackPersistenceError(msg) from e
    # Dev: warn (was DEBUG pre-0.17) so it's visible but non-fatal.
    logger.warning(msg)


def insert_feedback(correlation_id: str, rating: str, comment: str | None) -> None:
    """Upsert one feedback row per correlation_id. rating must be 'up' or 'down'."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    url = _get_db_url()
    if not url:
        _handle_missing_db_url("feedback")
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
        _handle_missing_db_url("source feedback")
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


def insert_llm_performance_feedback(correlation_id: str, rating: str, comment: str | None) -> None:
    """Upsert LLM performance (model routing) feedback — separate from answer-quality chat_feedback."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    url = _get_db_url()
    if not url:
        _handle_missing_db_url("LLM performance feedback")
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO llm_performance_feedback (correlation_id, rating, comment, created_at)
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
        err = str(e).lower()
        if "llm_performance_feedback" in err or ("relation" in err and "does not exist" in err):
            _handle_missing_relation("llm_performance_feedback", "024", e)
            return
        logger.exception("Failed to persist LLM performance feedback: %s", e)
        raise


def get_llm_performance_feedback(correlation_id: str) -> dict[str, Any] | None:
    """Return { rating, comment } for routing/LLM performance panel, or None."""
    url = _get_db_url()
    if not url:
        return None
    try:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT rating, comment FROM llm_performance_feedback WHERE correlation_id = %s",
            (correlation_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        return {"rating": row["rating"], "comment": row["comment"]}
    except Exception as e:
        err = str(e).lower()
        if "llm_performance_feedback" in err or ("relation" in err and "does not exist" in err):
            return None
        logger.warning("get_llm_performance_feedback failed: %s", e)
        return None


def insert_adjudication_feedback(correlation_id: str, rating: str, comment: str | None) -> None:
    """Upsert adjudicator / QA scorecard feedback (separate from answer-quality chat_feedback)."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    url = _get_db_url()
    if not url:
        _handle_missing_db_url("adjudication feedback")
        return
    try:
        import psycopg2

        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO adjudication_feedback (correlation_id, rating, comment, created_at)
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
        err = str(e).lower()
        if "adjudication_feedback" in err or ("relation" in err and "does not exist" in err):
            _handle_missing_relation("adjudication_feedback", "025", e)
            return
        logger.exception("Failed to persist adjudication feedback: %s", e)
        raise


def get_adjudication_feedback(correlation_id: str) -> dict[str, Any] | None:
    """Return { rating, comment } for QA scorecard panel, or None."""
    url = _get_db_url()
    if not url:
        return None
    try:
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT rating, comment FROM adjudication_feedback WHERE correlation_id = %s",
            (correlation_id,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        return {"rating": row["rating"], "comment": row["comment"]}
    except Exception as e:
        err = str(e).lower()
        if "adjudication_feedback" in err or ("relation" in err and "does not exist" in err):
            return None
        logger.warning("get_adjudication_feedback failed: %s", e)
        return None


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
