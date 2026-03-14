"""Result cache layer: store and retrieve last tool payload per (thread_id, tool_hint).

Enables follow-up messages like 'filter those results by Florida' to access the
structured output from the prior tool call without re-running it.

Schema: db/schema/019_chat_tool_results.sql
TTL: 30 minutes (enforced in query; no background eviction needed for Postgres).
"""
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

RESULT_TTL_MINUTES = 30
_MAX_RESULT_BLOCK_CHARS = 3200  # ~800 tokens


def _get_db_url() -> str:
    from app.chat_config import get_chat_config
    return (get_chat_config().rag.database_url or "").strip()


def save_tool_result(
    thread_id: str,
    turn_id: str,
    tool_hint: str,
    result: Any,
) -> None:
    """Upsert last tool result for (thread_id, tool_hint). Called after each successful tool execution."""
    url = _get_db_url()
    if not url or not thread_id or not tool_hint:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        payload = json.dumps(result, default=str)
        cur.execute(
            """
            INSERT INTO chat_tool_results (thread_id, turn_id, tool_hint, payload, created_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (thread_id, tool_hint) DO UPDATE SET
                turn_id    = EXCLUDED.turn_id,
                payload    = EXCLUDED.payload,
                created_at = EXCLUDED.created_at
            """,
            (thread_id, turn_id, tool_hint, payload),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning("save_tool_result failed (non-fatal): %s", e)


def get_tool_result(thread_id: str, tool_hint: str) -> dict[str, Any] | None:
    """Return last cached result for (thread_id, tool_hint) within TTL, or None."""
    url = _get_db_url()
    if not url or not thread_id or not tool_hint:
        return None
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT payload, created_at FROM chat_tool_results
            WHERE thread_id = %s AND tool_hint = %s
              AND created_at > NOW() - INTERVAL '%s minutes'
            """,
            (thread_id, tool_hint, RESULT_TTL_MINUTES),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        raw = row["payload"]
        if isinstance(raw, str):
            return json.loads(raw)
        return dict(raw) if raw else None
    except Exception as e:
        logger.debug("get_tool_result failed (non-fatal): %s", e)
        return None


def clear_tool_results(thread_id: str) -> None:
    """Delete all cached results for a thread. Called on STANDALONE route (spec §5.7 / §11 Q3)."""
    url = _get_db_url()
    if not url or not thread_id:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute("DELETE FROM chat_tool_results WHERE thread_id = %s", (thread_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning("clear_tool_results failed (non-fatal): %s", e)


def format_cached_result(tool_hint: str, result: dict[str, Any]) -> str:
    """Compact representation of a cached tool result for planner context injection.
    Total output capped at _MAX_RESULT_BLOCK_CHARS (~800 tokens).
    """
    count = result.get("count") or len(result.get("rows", []))
    label = tool_hint.replace("_", " ").title()
    rows = (result.get("rows") or [])[:5]
    header = f"Last {label} result ({count} total):"
    row_lines = [f"  - {json.dumps(r, default=str)}" for r in rows]

    # Build block and truncate rows until it fits the size cap
    block = "\n".join([header] + row_lines)
    if len(block) > _MAX_RESULT_BLOCK_CHARS:
        for limit in range(len(row_lines) - 1, -1, -1):
            trimmed = [header] + row_lines[:limit] + ["  ... (truncated)"]
            block = "\n".join(trimmed)
            if len(block) <= _MAX_RESULT_BLOCK_CHARS:
                break
        else:
            block = (header + "\n  ... (truncated)")[:_MAX_RESULT_BLOCK_CHARS]
    return block
