"""Persist chat turns (question, thinking_log, final_message, sources, metadata) for history and left panel.
Uses CHAT_RAG_DATABASE_URL (same DB as chat_feedback)."""
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _get_db_url() -> str:
    from app.chat_config import get_chat_config
    return (get_chat_config().rag.database_url or "").strip()


def insert_turn(
    correlation_id: str,
    question: str,
    thinking_log: list[str],
    final_message: str,
    sources: list[dict[str, Any]],
    duration_ms: int | None,
    model_used: str | None,
    llm_provider: str | None,
    session_id: str | None = None,
    thread_id: str | None = None,
    plan_snapshot: dict[str, Any] | None = None,
    blueprint_snapshot: dict[str, Any] | None = None,
    agent_cards: list[dict[str, Any]] | None = None,
    source_confidence_strip: str | None = None,
    config_sha: str | None = None,
) -> None:
    """Insert one turn row. Called by worker when response is complete. config_sha ties run to prompts+LLM config version."""
    url = _get_db_url()
    if not url:
        logger.warning("CHAT_RAG_DATABASE_URL not set; turn not persisted")
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        strip_val = (source_confidence_strip or "").strip() or None
        thread_val = (thread_id or "").strip() or None
        config_sha_val = (config_sha or "").strip() or None
        cur.execute(
            """
            INSERT INTO chat_turns (
                correlation_id, question, thinking_log, final_message, sources,
                duration_ms, model_used, llm_provider, session_id, thread_id,
                plan_snapshot, blueprint_snapshot, agent_cards, source_confidence_strip, config_sha
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (correlation_id) DO UPDATE SET
                question = EXCLUDED.question,
                thinking_log = EXCLUDED.thinking_log,
                final_message = EXCLUDED.final_message,
                sources = EXCLUDED.sources,
                duration_ms = EXCLUDED.duration_ms,
                model_used = EXCLUDED.model_used,
                llm_provider = EXCLUDED.llm_provider,
                session_id = EXCLUDED.session_id,
                thread_id = EXCLUDED.thread_id,
                plan_snapshot = EXCLUDED.plan_snapshot,
                blueprint_snapshot = EXCLUDED.blueprint_snapshot,
                agent_cards = EXCLUDED.agent_cards,
                source_confidence_strip = EXCLUDED.source_confidence_strip,
                config_sha = EXCLUDED.config_sha
            """,
            (
                correlation_id,
                (question or "").strip() or "",
                json.dumps(thinking_log or []),
                (final_message or "").strip() or None,
                json.dumps(sources or []),
                duration_ms,
                (model_used or "").strip() or None,
                (llm_provider or "").strip() or None,
                (session_id or "").strip() or None,
                thread_val,
                json.dumps(plan_snapshot) if plan_snapshot is not None else None,
                json.dumps(blueprint_snapshot) if blueprint_snapshot is not None else None,
                json.dumps(agent_cards) if agent_cards is not None else None,
                strip_val,
                config_sha_val,
            ),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Failed to persist turn: %s", e)
        raise


def get_last_turn_sources(thread_id: str, limit_turns: int = 2) -> list[dict[str, Any]]:
    """Return sources (document_id, document_name) from last N turns in thread for continuity.
    Dedupes by document_id. Used by planner context and retriever include_document_ids."""
    if not (thread_id or "").strip():
        return []
    url = _get_db_url()
    if not url:
        return []
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT sources
            FROM chat_turns
            WHERE thread_id = %s AND sources IS NOT NULL AND sources != '[]'::jsonb
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (thread_id.strip(), max(1, min(limit_turns, 5))),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for r in rows:
            raw = r.get("sources")
            if raw is None:
                continue
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
            if not isinstance(raw, list):
                continue
            for elem in raw:
                if not isinstance(elem, dict):
                    continue
                doc_id = elem.get("document_id")
                if not doc_id or not str(doc_id).strip():
                    continue
                doc_id_str = str(doc_id).strip()
                if doc_id_str in seen:
                    continue
                seen.add(doc_id_str)
                out.append({
                    "document_id": doc_id_str,
                    "document_name": (elem.get("document_name") or "document") or "document",
                })
        return out
    except Exception as e:
        logger.warning("Failed to get last turn sources: %s", e)
        return []


def get_recent_turns(limit: int = 10) -> list[dict[str, Any]]:
    """Return list of recent turns: { correlation_id, question, created_at }."""
    url = _get_db_url()
    if not url:
        return []
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT correlation_id, question, created_at
            FROM chat_turns
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (max(1, min(limit, 100)),),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [
            {
                "correlation_id": r["correlation_id"],
                "question": r["question"] or "",
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Failed to get recent turns: %s", e)
        return []


def get_most_helpful_turns(limit: int = 10) -> list[dict[str, Any]]:
    """Return turns that have feedback rating = 'up', same shape as get_recent_turns."""
    url = _get_db_url()
    if not url:
        return []
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT t.correlation_id, t.question, t.created_at
            FROM chat_turns t
            INNER JOIN chat_feedback f ON f.correlation_id = t.correlation_id AND f.rating = 'up'
            ORDER BY t.created_at DESC
            LIMIT %s
            """,
            (max(1, min(limit, 100)),),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [
            {
                "correlation_id": r["correlation_id"],
                "question": r["question"] or "",
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Failed to get most helpful turns: %s", e)
        return []


def get_most_helpful_documents(limit: int = 10) -> list[dict[str, Any]]:
    """From turns with feedback up, list documents by how many distinct liked turns featured them. Returns document_name and one document_id when present in sources."""
    url = _get_db_url()
    if not url:
        return []
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            WITH liked_docs AS (
                SELECT DISTINCT t.correlation_id, elem->>'document_name' AS document_name, elem->>'document_id' AS document_id
                FROM chat_turns t
                INNER JOIN chat_feedback f ON f.correlation_id = t.correlation_id AND f.rating = 'up'
                CROSS JOIN LATERAL jsonb_array_elements(COALESCE(t.sources, '[]'::jsonb)) AS elem
                WHERE elem->>'document_name' IS NOT NULL AND (elem->>'document_name') != ''
            )
            SELECT document_name, MAX(NULLIF(TRIM(document_id), '')) AS document_id, COUNT(*) AS cited_in_count
            FROM liked_docs
            GROUP BY document_name
            ORDER BY COUNT(*) DESC, document_name
            LIMIT %s
            """,
            (max(1, min(limit, 100)),),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [
            {
                "document_name": r["document_name"] or "",
                "document_id": r["document_id"] if r.get("document_id") else None,
                "cited_in_count": int(r["cited_in_count"]) if r.get("cited_in_count") is not None else 0,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Failed to get most helpful documents: %s", e)
        return []
