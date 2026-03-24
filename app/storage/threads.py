"""Short-term memory: threads, message-level transcript, and state per thread.
Uses CHAT_RAG_DATABASE_URL (same DB as chat_turns).

Product rules (write in comments):
- State is not truth. It is a convenience. If the user contradicts it, user wins instantly.
- State should decay quickly. If it lingers, it becomes wrong more often than right.
"""
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Jurisdiction dimensions: state, payor, program, perspective, regulatory_agency
DEFAULT_JURISDICTION: dict[str, Any] = {
    "state": None,
    "payor": None,
    "program": None,
    "perspective": None,  # "provider_office" | "patient"
    "regulatory_agency": None,
}

DEFAULT_STATE: dict[str, Any] = {
    "active": {
        "payer": None,
        "program": None,
        "domain": None,
        "jurisdiction": None,  # legacy: state string; when dict, use DEFAULT_JURISDICTION shape
        "user_role": None,
        "jurisdiction_obj": None,  # explicit: {state, payor, program, perspective, regulatory_agency}
    },
    "open_slots": [],
    "resolved_slots": {},  # Improvement 3: persisted slot values {state, payer, program, ...}
    "recent_entities": [],
    "last_user_intent": None,
    "last_updated_turn_id": None,
    "safety": {"patient_allowed": False},
    "refined_query": None,  # Canonical question: updated on slot fill, replaced on new question
    "master_objective": None,  # Relentless continuity: {id, status, summary, sub_objectives, ...}
}


def _get_db_url() -> str:
    from app.chat_config import get_chat_config
    return (get_chat_config().rag.database_url or "").strip()


def ensure_thread(thread_id: str | None) -> str:
    """Ensure a row exists in chat_threads: if thread_id is None, create new; if provided, INSERT ON CONFLICT DO NOTHING so FK is satisfied."""
    url = _get_db_url()
    if not url:
        logger.warning("CHAT_RAG_DATABASE_URL not set; creating in-memory thread id only")
        return str(uuid.uuid4()) if thread_id is None else thread_id
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        id_to_use = (thread_id or "").strip() or None
        if id_to_use is None:
            id_to_use = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO chat_threads (thread_id, created_at, updated_at) VALUES (%s, now(), now()) ON CONFLICT (thread_id) DO NOTHING",
            (id_to_use,),
        )
        conn.commit()
        cur.close()
        conn.close()
        return id_to_use
    except Exception as e:
        logger.exception("Failed to ensure thread: %s", e)
        return str(uuid.uuid4())


def append_user_message(thread_id: str, turn_id: str, content: str) -> None:
    """Insert one user message row. Call at start of process_one."""
    url = _get_db_url()
    if not url:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chat_turn_messages (turn_id, thread_id, role, content, created_at)
            VALUES (%s, %s, 'user', %s, now())
            ON CONFLICT (turn_id, role) DO UPDATE SET content = EXCLUDED.content, created_at = now()
            """,
            (turn_id, thread_id, (content or "").strip() or ""),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Failed to append user message: %s", e)
        raise


def append_assistant_message(thread_id: str, turn_id: str, content: str) -> None:
    """Insert one assistant message row. Call at end of process_one."""
    url = _get_db_url()
    if not url:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chat_turn_messages (turn_id, thread_id, role, content, created_at)
            VALUES (%s, %s, 'assistant', %s, now())
            ON CONFLICT (turn_id, role) DO UPDATE SET content = EXCLUDED.content, created_at = now()
            """,
            (turn_id, thread_id, (content or "").strip() or ""),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Failed to append assistant message: %s", e)
        raise


def append_turn_messages(
    thread_id: str,
    turn_id: str,
    user_content: str,
    assistant_content: str,
) -> None:
    """INSERT two rows into chat_turn_messages. Prefer append_user_message + append_assistant_message for split timing."""
    append_user_message(thread_id, turn_id, user_content)
    append_assistant_message(thread_id, turn_id, assistant_content)


def get_last_turn_messages(thread_id: str, limit_turns: int = 2) -> list[dict[str, Any]]:
    """Return last N full turns for thread_id, newest first.
    Each item: { turn_id, user_content, assistant_content, context_summary, created_at }.
    context_summary is joined from chat_turns for structured planner context (Improvement 1).
    Falls back to None when column doesn't exist yet (pre-migration).
    """
    url = _get_db_url()
    if not url:
        return []
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Try to join context_summary from chat_turns; silently fall back if column absent.
        try:
            cur.execute(
                """
                WITH pairs AS (
                    SELECT m.turn_id,
                           max(m.created_at) AS created_at,
                           max(CASE WHEN m.role = 'user' THEN m.content END) AS user_content,
                           max(CASE WHEN m.role = 'assistant' THEN m.content END) AS assistant_content
                    FROM chat_turn_messages m
                    WHERE m.thread_id = %s
                    GROUP BY m.turn_id
                )
                SELECT p.turn_id, p.user_content, p.assistant_content, p.created_at,
                       ct.context_summary
                FROM pairs p
                LEFT JOIN chat_turns ct ON ct.correlation_id = p.turn_id
                WHERE p.user_content IS NOT NULL AND p.assistant_content IS NOT NULL
                ORDER BY p.created_at DESC
                LIMIT %s
                """,
                (thread_id, limit_turns),
            )
        except Exception:
            # context_summary column may not exist pre-migration — fall back to simple query
            conn.rollback()
            cur.execute(
                """
                WITH pairs AS (
                    SELECT turn_id,
                           max(created_at) AS created_at,
                           max(CASE WHEN role = 'user' THEN content END) AS user_content,
                           max(CASE WHEN role = 'assistant' THEN content END) AS assistant_content
                    FROM chat_turn_messages
                    WHERE thread_id = %s
                    GROUP BY turn_id
                )
                SELECT turn_id, user_content, assistant_content, created_at
                FROM pairs
                WHERE user_content IS NOT NULL AND assistant_content IS NOT NULL
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (thread_id, limit_turns),
            )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Failed to get last turn messages: %s", e)
        return []


def get_state(thread_id: str) -> dict[str, Any] | None:
    """Return state_json for thread_id, or None if no row. Caller can merge with DEFAULT_STATE."""
    url = _get_db_url()
    if not url:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute("SELECT state_json FROM chat_state WHERE thread_id = %s", (thread_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row is None:
            return None
        raw = row[0]
        if isinstance(raw, str):
            return json.loads(raw)
        return dict(raw) if raw else None
    except Exception as e:
        logger.warning("Failed to get state: %s", e)
        return None


def save_state(thread_id: str, patch: dict[str, Any]) -> None:
    """Read current state (or default), apply patch shallowly, increment state_version, write back."""
    url = _get_db_url()
    if not url:
        logger.warning("CHAT_RAG_DATABASE_URL not set; state not persisted")
        return
    current = get_state(thread_id)
    if current is None:
        current = json.loads(json.dumps(DEFAULT_STATE))
    for k, v in patch.items():
        if isinstance(current.get(k), dict) and isinstance(v, dict):
            current[k] = {**current.get(k, {}), **v}
        else:
            current[k] = v
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chat_state (thread_id, state_json, state_version, updated_at)
            VALUES (%s, %s, 1, now())
            ON CONFLICT (thread_id) DO UPDATE SET
                state_json = EXCLUDED.state_json,
                state_version = chat_state.state_version + 1,
                updated_at = now()
            """,
            (thread_id, json.dumps(current),),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Failed to save state: %s", e)
        raise


def save_state_full(thread_id: str, state: dict[str, Any]) -> None:
    """Replace state entirely (no merge). Use with ThreadState.to_dict() for explicit state model."""
    url = _get_db_url()
    if not url:
        logger.warning("CHAT_RAG_DATABASE_URL not set; state not persisted")
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO chat_state (thread_id, state_json, state_version, updated_at)
            VALUES (%s, %s, 1, now())
            ON CONFLICT (thread_id) DO UPDATE SET
                state_json = EXCLUDED.state_json,
                state_version = chat_state.state_version + 1,
                updated_at = now()
            """,
            (thread_id, json.dumps(state),),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Failed to save state: %s", e)
        raise


_MAX_THREAD_UPLOAD_RECORDS = 15


def append_uploaded_file_record(thread_id: str, record: dict[str, Any]) -> bool:
    """Prepend an upload record to active.uploaded_files (capped). For roster_reconciliation,
    also refreshes reconciliation_upload_id / org_id / org_name so the next message can run the tool.
    Returns False if state could not be persisted (e.g. database URL unset)."""
    url = _get_db_url()
    if not url:
        logger.warning("CHAT_RAG_DATABASE_URL not set; upload list not persisted")
        return False
    current = get_state(thread_id)
    if current is None:
        current = json.loads(json.dumps(DEFAULT_STATE))
    active = {**(current.get("active") or {})}
    prev = active.get("uploaded_files") or []
    files: list[dict[str, Any]] = [dict(x) for x in prev if isinstance(x, dict)]
    files.insert(0, dict(record))
    active["uploaded_files"] = files[:_MAX_THREAD_UPLOAD_RECORDS]
    if (record.get("purpose") or "").strip() == "roster_reconciliation":
        active["reconciliation_upload_id"] = (record.get("upload_id") or "").strip()
        active["reconciliation_org_id"] = (record.get("org_id") or "").strip()
        active["reconciliation_org_name"] = (record.get("org_name") or "").strip()
    save_state(thread_id, {"active": active})
    return True


def register_open_slots(thread_id: str, slots: list[str]) -> None:
    """Set state.open_slots to slots (replace), increment state_version, save."""
    from app.state.model import ThreadState

    raw = get_state(thread_id)
    thread_state = ThreadState.from_dict(raw)
    thread_state.apply_delta({"open_slots": list(slots) if slots else []})
    save_state_full(thread_id, thread_state.to_dict())
