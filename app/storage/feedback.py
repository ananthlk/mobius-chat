"""Persist chat feedback (thumbs up/down + optional comment) in PostgreSQL.

All DB access flows through ``app.db_client`` → mobius-db-agent MCP server.
The agent handles pooling, access control, and structured errors; this module
keeps the dev/hosted fail-closed policy described below.

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

db-agent refactor (2026-04-19)
------------------------------
Swapped all psycopg2 code for ``db_query`` / ``db_execute``. The fail-closed
semantics are preserved: ``connection_error`` from the agent now triggers
``_handle_missing_db_url``; ``relation_missing`` for migration-gated tables
triggers ``_handle_missing_relation``.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from app.db_client import db_execute, db_query

logger = logging.getLogger(__name__)

_DB = "chat"


class FeedbackPersistenceError(RuntimeError):
    """Raised in non-dev when a feedback write can't reach its target.

    Storage-layer exception; the router catches nothing by default so
    FastAPI returns 500 to the caller. Dev callers never see this —
    the storage fn degrades to a log + return when ``CHAT_ENV=dev``.
    """


def _env_is_hosted() -> bool:
    """True when we're in staging or prod (missing persistence = hard error)."""
    env = (os.environ.get("CHAT_ENV") or "dev").strip().lower()
    return env not in ("dev", "development", "local")


def _handle_missing_db_url(kind: str) -> None:
    """Called when the agent reports ``connection_error``. Raises in hosted envs.

    Before the db-agent refactor this was triggered by ``CHAT_RAG_DATABASE_URL``
    being unset. Under the agent, a connection failure (URL missing, pool down,
    DB unreachable) surfaces as ``error.code == "connection_error"`` — same
    operational meaning, so we route it through the same handler and keep the
    legacy env-var name in the message so existing runbooks / log searches
    still match.
    """
    msg = f"CHAT_RAG_DATABASE_URL not set (or db-agent unreachable); {kind} cannot be persisted"
    if _env_is_hosted():
        logger.error("[fail-closed] %s (CHAT_ENV=%r)", msg, os.environ.get("CHAT_ENV"))
        raise FeedbackPersistenceError(msg)
    logger.warning(msg)


def _handle_missing_relation(kind: str, migration_num: str, err: object) -> None:
    """Called when the agent reports ``relation_missing``. Raises in hosted envs.

    ``migration_num`` is the chat DB migration that creates the table
    (024 for llm_performance_feedback, 025 for adjudication_feedback).
    ``err`` accepts either a string or an Exception (pre-refactor signature).
    """
    msg = (
        f"{kind} table missing — run chat DB migration {migration_num}. "
        f"Underlying error: {err}"
    )
    if _env_is_hosted():
        logger.error("[fail-closed] %s", msg)
        if isinstance(err, BaseException):
            raise FeedbackPersistenceError(msg) from err
        raise FeedbackPersistenceError(msg)
    logger.warning(msg)


def _err_code(result: dict) -> str | None:
    """Return error["code"] if the response is an error, else None."""
    err = result.get("error")
    if isinstance(err, dict):
        return err.get("code")
    return None


def _err_message(result: dict) -> str:
    err = result.get("error") or {}
    if isinstance(err, dict):
        return err.get("message", "") or ""
    return str(err)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def insert_feedback(correlation_id: str, rating: str, comment: str | None) -> None:
    """Upsert one feedback row per correlation_id. rating must be 'up' or 'down'."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    result = db_execute(
        """
        INSERT INTO chat_feedback (correlation_id, rating, comment, created_at)
        VALUES (:cid, :rating, :comment, now())
        ON CONFLICT (correlation_id) DO UPDATE SET
            rating = EXCLUDED.rating,
            comment = EXCLUDED.comment,
            created_at = now()
        """,
        _DB,
        params={
            "cid": correlation_id,
            "rating": rating,
            "comment": (comment or "").strip() or None,
        },
    )
    code = _err_code(result)
    if code is None:
        return
    if code == "connection_error":
        _handle_missing_db_url("feedback")
        return
    logger.exception("Failed to persist feedback: %s", _err_message(result))
    raise RuntimeError(_err_message(result))


def insert_source_feedback(correlation_id: str, source_index: int, rating: str) -> None:
    """Upsert one source feedback row. source_index is 1-based. rating must be 'up' or 'down'."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    if source_index < 1:
        raise ValueError("source_index must be >= 1")
    result = db_execute(
        """
        INSERT INTO chat_source_feedback (correlation_id, source_index, rating, created_at)
        VALUES (:cid, :idx, :rating, now())
        ON CONFLICT (correlation_id, source_index) DO UPDATE SET
            rating = EXCLUDED.rating,
            created_at = now()
        """,
        _DB,
        params={"cid": correlation_id, "idx": source_index, "rating": rating},
    )
    code = _err_code(result)
    if code is None:
        return
    if code == "connection_error":
        _handle_missing_db_url("source feedback")
        return
    logger.exception("Failed to persist source feedback: %s", _err_message(result))
    raise RuntimeError(_err_message(result))


def insert_llm_performance_feedback(correlation_id: str, rating: str, comment: str | None) -> None:
    """Upsert LLM performance (model routing) feedback — separate from answer-quality chat_feedback."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    result = db_execute(
        """
        INSERT INTO llm_performance_feedback (correlation_id, rating, comment, created_at)
        VALUES (:cid, :rating, :comment, now())
        ON CONFLICT (correlation_id) DO UPDATE SET
            rating = EXCLUDED.rating,
            comment = EXCLUDED.comment,
            created_at = now()
        """,
        _DB,
        params={
            "cid": correlation_id,
            "rating": rating,
            "comment": (comment or "").strip() or None,
        },
    )
    code = _err_code(result)
    if code is None:
        return
    if code == "connection_error":
        _handle_missing_db_url("LLM performance feedback")
        return
    if code == "relation_missing":
        _handle_missing_relation("llm_performance_feedback", "024", _err_message(result))
        return
    logger.exception("Failed to persist LLM performance feedback: %s", _err_message(result))
    raise RuntimeError(_err_message(result))


def insert_adjudication_feedback(correlation_id: str, rating: str, comment: str | None) -> None:
    """Upsert adjudicator / QA scorecard feedback (separate from answer-quality chat_feedback)."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    result = db_execute(
        """
        INSERT INTO adjudication_feedback (correlation_id, rating, comment, created_at)
        VALUES (:cid, :rating, :comment, now())
        ON CONFLICT (correlation_id) DO UPDATE SET
            rating = EXCLUDED.rating,
            comment = EXCLUDED.comment,
            created_at = now()
        """,
        _DB,
        params={
            "cid": correlation_id,
            "rating": rating,
            "comment": (comment or "").strip() or None,
        },
    )
    code = _err_code(result)
    if code is None:
        return
    if code == "connection_error":
        _handle_missing_db_url("adjudication feedback")
        return
    if code == "relation_missing":
        _handle_missing_relation("adjudication_feedback", "025", _err_message(result))
        return
    logger.exception("Failed to persist adjudication feedback: %s", _err_message(result))
    raise RuntimeError(_err_message(result))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _row_to_dict(result: dict) -> dict | None:
    """Single-row helper. Returns first row as dict, or None."""
    if _err_code(result) is not None:
        return None
    rows = result.get("rows") or []
    if not rows:
        return None
    cols = result.get("columns") or []
    return dict(zip(cols, rows[0]))


def get_feedback(correlation_id: str) -> dict[str, Any] | None:
    """Return { rating, comment } or None if no feedback for this correlation_id."""
    result = db_query(
        "SELECT rating, comment FROM chat_feedback WHERE correlation_id = :cid",
        _DB,
        params={"cid": correlation_id},
    )
    if _err_code(result) is not None:
        logger.warning("get_feedback failed: %s", _err_message(result))
        return None
    row = _row_to_dict(result)
    if row is None:
        return None
    return {"rating": row["rating"], "comment": row["comment"]}


def get_source_feedback(correlation_id: str) -> list[dict[str, Any]]:
    """Return list of { source_index, rating } for this turn. Empty if none."""
    result = db_query(
        "SELECT source_index, rating FROM chat_source_feedback "
        "WHERE correlation_id = :cid ORDER BY source_index",
        _DB,
        params={"cid": correlation_id},
    )
    if _err_code(result) is not None:
        logger.warning("get_source_feedback failed: %s", _err_message(result))
        return []
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(zip(cols, r))
        out.append({"source_index": int(d["source_index"]), "rating": d["rating"]})
    return out


def get_llm_performance_feedback(correlation_id: str) -> dict[str, Any] | None:
    """Return { rating, comment } for routing/LLM performance panel, or None."""
    result = db_query(
        "SELECT rating, comment FROM llm_performance_feedback WHERE correlation_id = :cid",
        _DB,
        params={"cid": correlation_id},
    )
    code = _err_code(result)
    if code == "relation_missing":
        # Migration 024 not applied — read path is silent-None in both envs
        # (reads never fail-close; only writes do).
        return None
    if code is not None:
        logger.warning("get_llm_performance_feedback failed: %s", _err_message(result))
        return None
    row = _row_to_dict(result)
    if row is None:
        return None
    return {"rating": row["rating"], "comment": row["comment"]}


def get_adjudication_feedback(correlation_id: str) -> dict[str, Any] | None:
    """Return { rating, comment } for QA scorecard panel, or None."""
    result = db_query(
        "SELECT rating, comment FROM adjudication_feedback WHERE correlation_id = :cid",
        _DB,
        params={"cid": correlation_id},
    )
    code = _err_code(result)
    if code == "relation_missing":
        return None
    if code is not None:
        logger.warning("get_adjudication_feedback failed: %s", _err_message(result))
        return None
    row = _row_to_dict(result)
    if row is None:
        return None
    return {"rating": row["rating"], "comment": row["comment"]}
