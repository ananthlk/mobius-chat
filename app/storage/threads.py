"""Short-term memory: threads, message-level transcript, and state per thread.

All DB access flows through ``app.db_client`` → mobius-db-agent MCP server.
Same semantics as before the db-agent refactor: dev-friendly graceful
fallbacks, no hard-fails on missing migrations, UUID-based thread id
minting when no DB is reachable.

Product rules (write in comments):
- State is not truth. It is a convenience. If the user contradicts it, user wins instantly.
- State should decay quickly. If it lingers, it becomes wrong more often than right.
"""
import json
import logging
import uuid
from typing import Any

from app.db_client import db_execute, db_query

logger = logging.getLogger(__name__)

_DB = "chat"

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
        "jurisdiction_obj": None,
    },
    "open_slots": [],
    "resolved_slots": {},
    "recent_entities": [],
    "last_user_intent": None,
    "last_updated_turn_id": None,
    "safety": {"patient_allowed": False},
    "refined_query": None,
    "master_objective": None,
}


# -------------------------------------------------------------------
# Agent-response helpers (shared with turns.py shape)
# -------------------------------------------------------------------


from app.db_client import _err_code, _err_message  # noqa: E402, F401 — shared helpers


def _rows_as_dicts(result: dict) -> list[dict[str, Any]]:
    if _err_code(result) is not None:
        return []
    cols = result.get("columns") or []
    return [dict(zip(cols, r)) for r in (result.get("rows") or [])]


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, str):
        return val
    iso = getattr(val, "isoformat", None)
    if callable(iso):
        return iso()
    return str(val)


def _decode_jsonb(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    return raw


def _is_connection_error(result: dict) -> bool:
    return _err_code(result) == "connection_error"


# -------------------------------------------------------------------
# Threads
# -------------------------------------------------------------------


def ensure_thread(thread_id: str | None) -> str:
    """Ensure a row exists in chat_threads. Returns the id used.

    If DB is unreachable, mints a UUID (or echoes the input) so callers
    get a stable id and in-memory flows keep working.
    """
    id_to_use = (thread_id or "").strip() or str(uuid.uuid4())
    result = db_execute(
        "INSERT INTO chat_threads (thread_id, created_at, updated_at) "
        "VALUES (:tid, now(), now()) ON CONFLICT (thread_id) DO NOTHING",
        _DB,
        params={"tid": id_to_use},
    )
    code = _err_code(result)
    if code is None:
        return id_to_use
    if code == "connection_error":
        logger.warning("db-agent unreachable; creating in-memory thread id only")
        return id_to_use if thread_id else str(uuid.uuid4())
    logger.exception("Failed to ensure thread: %s", _err_message(result))
    # Historical behavior: any failure falls through to a new UUID rather
    # than raising; we keep that so callers never see exceptions here.
    return str(uuid.uuid4())


def set_thread_title_if_empty(thread_id: str, question: str) -> None:
    """Phase 2.3: set the sidebar title on a thread's first turn.

    Only updates when ``title IS NULL`` — later turns don't overwrite the
    first-message-derived title. ``turn_count`` is also incremented here so
    a single write covers both fields.
    """
    tid = (thread_id or "").strip()
    if not tid:
        return
    from app.storage.thread_title import generate_thread_title
    title = generate_thread_title(question or "")

    result = db_execute(
        """
        UPDATE chat_threads
        SET title = COALESCE(title, :title),
            turn_count = turn_count + 1,
            updated_at = now()
        WHERE thread_id = :tid
        """,
        _DB,
        params={"title": title, "tid": tid},
    )
    code = _err_code(result)
    if code is None or code == "connection_error":
        return
    err = _err_message(result).lower()
    if code == "column_missing" or "title" in err or "turn_count" in err or "column" in err:
        logger.debug(
            "chat_threads.title/turn_count missing (run migration 030); skipping title update"
        )
        return
    logger.warning("Failed to set thread title: %s", _err_message(result))


def get_recent_threads(limit: int = 10) -> list[dict[str, Any]]:
    """Return distinct threads for the sidebar: ``[{thread_id, title, updated_at, turn_count}]``.

    2026-04-24: the pre-2026-04-24 query filtered ``WHERE title IS NOT NULL``
    which silently excluded every thread on dev because
    ``set_thread_title_if_empty`` was not persisting the title column (root
    cause of that write bug is still under investigation — the UPDATE call
    returns no error but the column stays NULL). To unblock the sidebar
    in the meantime, this query now LEFT JOINs the first turn's question
    as a fallback title and also derives live ``turn_count`` from the
    chat_turns table so the sidebar never depends on the (buggy) stamped
    values. When the write path is fixed, the stamped ``title`` /
    ``turn_count`` will simply take precedence via COALESCE.

    Threads with zero persisted turns are still excluded — the sidebar
    should not surface empty shells created by an aborted request.
    """
    result = db_query(
        """
        WITH first_turn AS (
            SELECT DISTINCT ON (thread_id)
                   thread_id, question
            FROM chat_turns
            WHERE thread_id IS NOT NULL
            ORDER BY thread_id, created_at ASC
        ),
        turn_counts AS (
            SELECT thread_id, COUNT(*) AS n
            FROM chat_turns
            WHERE thread_id IS NOT NULL
            GROUP BY thread_id
        )
        SELECT t.thread_id,
               COALESCE(NULLIF(t.title, ''), ft.question, 'Untitled thread') AS title,
               t.updated_at,
               COALESCE(NULLIF(t.turn_count, 0), tc.n, 0) AS turn_count
        FROM chat_threads t
        LEFT JOIN first_turn  ft ON ft.thread_id = t.thread_id
        LEFT JOIN turn_counts tc ON tc.thread_id = t.thread_id
        WHERE tc.n IS NOT NULL AND tc.n > 0
        ORDER BY t.updated_at DESC
        LIMIT :lim
        """,
        _DB,
        params={"lim": max(1, min(limit, 100))},
    )
    code = _err_code(result)
    if code is not None:
        err = _err_message(result).lower()
        if code == "column_missing" or "title" in err or "turn_count" in err or "column" in err:
            logger.debug(
                "chat_threads.title/turn_count missing (run migration 030); returning empty thread list"
            )
            return []
        logger.warning("Failed to get recent threads: %s", _err_message(result))
        return []
    return [
        {
            "thread_id": str(r["thread_id"]),
            "title": (r.get("title") or "").strip() or "Untitled thread",
            "updated_at": _iso(r.get("updated_at")),
            "turn_count": int(r.get("turn_count") or 0),
        }
        for r in _rows_as_dicts(result)
    ]


# -------------------------------------------------------------------
# Messages
# -------------------------------------------------------------------


def _insert_message(thread_id: str, turn_id: str, role: str, content: str) -> None:
    tid = (thread_id or "").strip()
    if not tid:
        return
    result = db_execute(
        """
        INSERT INTO chat_turn_messages (turn_id, thread_id, role, content, created_at)
        VALUES (:turn_id, :tid, :role, :content, now())
        ON CONFLICT (turn_id, role) DO UPDATE SET content = EXCLUDED.content, created_at = now()
        """,
        _DB,
        params={
            "turn_id": turn_id,
            "tid": tid,
            "role": role,
            "content": (content or "").strip() or "",
        },
    )
    code = _err_code(result)
    if code is None or code == "connection_error":
        return
    logger.exception("Failed to append %s message: %s", role, _err_message(result))
    raise RuntimeError(_err_message(result))


def append_user_message(thread_id: str, turn_id: str, content: str) -> None:
    """Insert one user message row. Call at start of process_one."""
    tid = (thread_id or "").strip()
    if not tid:
        return
    ensure_thread(tid)
    _insert_message(tid, turn_id, "user", content)


def append_assistant_message(thread_id: str, turn_id: str, content: str) -> None:
    """Insert one assistant message row. Call at end of process_one."""
    _insert_message(thread_id, turn_id, "assistant", content)


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
    context_summary joined from chat_turns for structured planner context.
    Falls back to the simpler query when the column doesn't exist yet.
    """
    # Primary: with context_summary join
    result = db_query(
        """
        WITH pairs AS (
            SELECT m.turn_id,
                   max(m.created_at) AS created_at,
                   max(CASE WHEN m.role = 'user' THEN m.content END) AS user_content,
                   max(CASE WHEN m.role = 'assistant' THEN m.content END) AS assistant_content
            FROM chat_turn_messages m
            WHERE m.thread_id = :tid
            GROUP BY m.turn_id
        )
        SELECT p.turn_id, p.user_content, p.assistant_content, p.created_at,
               ct.context_summary
        FROM pairs p
        LEFT JOIN chat_turns ct ON ct.correlation_id = p.turn_id
        WHERE p.user_content IS NOT NULL AND p.assistant_content IS NOT NULL
        ORDER BY p.created_at DESC
        LIMIT :lim
        """,
        _DB,
        params={"tid": thread_id, "lim": limit_turns},
    )

    code = _err_code(result)
    if code is not None:
        err = _err_message(result).lower()
        if code == "column_missing" or "context_summary" in err or ("column" in err and "does not exist" in err):
            # Fallback: no context_summary join
            result = db_query(
                """
                WITH pairs AS (
                    SELECT turn_id,
                           max(created_at) AS created_at,
                           max(CASE WHEN role = 'user' THEN content END) AS user_content,
                           max(CASE WHEN role = 'assistant' THEN content END) AS assistant_content
                    FROM chat_turn_messages
                    WHERE thread_id = :tid
                    GROUP BY turn_id
                )
                SELECT turn_id, user_content, assistant_content, created_at
                FROM pairs
                WHERE user_content IS NOT NULL AND assistant_content IS NOT NULL
                ORDER BY created_at DESC
                LIMIT :lim
                """,
                _DB,
                params={"tid": thread_id, "lim": limit_turns},
            )
            if _err_code(result) is not None:
                logger.warning("Failed to get last turn messages: %s", _err_message(result))
                return []
        else:
            logger.warning("Failed to get last turn messages: %s", _err_message(result))
            return []

    out: list[dict[str, Any]] = []
    for r in _rows_as_dicts(result):
        # Normalize created_at to match prior dict-cursor behavior (native datetime).
        # Downstream consumers may pass this through .isoformat(); keep string-safe.
        row = dict(r)
        out.append(row)
    # 2026-04-26 diagnostic — Phase 13.6 surfaced empty last_turns
    # despite chat_turns rows existing. Log when the query returns
    # zero rows so we can tell "DB unhappy but caught" from "table
    # genuinely empty for this thread." Remove once root-caused.
    if not out:
        logger.info(
            "[phase13.6.diag] get_last_turn_messages: thread=%s rows=0 (chat_turn_messages may be empty for this thread)",
            (thread_id or "")[:8],
        )
    return out


# -------------------------------------------------------------------
# State
# -------------------------------------------------------------------


def get_state(thread_id: str) -> dict[str, Any] | None:
    """Return state_json for thread_id, or None if no row. Caller can merge with DEFAULT_STATE."""
    result = db_query(
        "SELECT state_json FROM chat_state WHERE thread_id = :tid",
        _DB,
        params={"tid": thread_id},
    )
    if _err_code(result) is not None:
        logger.warning("Failed to get state: %s", _err_message(result))
        return None
    rows = result.get("rows") or []
    if not rows:
        return None
    raw = rows[0][0]
    decoded = _decode_jsonb(raw)
    if isinstance(decoded, dict):
        return dict(decoded)
    return None


def _write_state_row(tid: str, state_json: str) -> None:
    """Shared UPSERT for chat_state. Raises on non-connection errors."""
    result = db_execute(
        """
        INSERT INTO chat_state (thread_id, state_json, state_version, updated_at)
        VALUES (:tid, CAST(:state_json AS jsonb), 1, now())
        ON CONFLICT (thread_id) DO UPDATE SET
            state_json = EXCLUDED.state_json,
            state_version = chat_state.state_version + 1,
            updated_at = now()
        """,
        _DB,
        params={"tid": tid, "state_json": state_json},
    )
    code = _err_code(result)
    if code is None:
        return
    if code == "connection_error":
        logger.warning("CHAT_RAG_DATABASE_URL not set (or db-agent unreachable); state not persisted")
        return
    logger.exception("Failed to save state: %s", _err_message(result))
    raise RuntimeError(_err_message(result))


def save_state(thread_id: str, patch: dict[str, Any]) -> None:
    """Read current state (or default), apply patch shallowly, increment state_version, write back."""
    tid = (thread_id or "").strip()
    if not tid:
        logger.warning("save_state called with empty thread_id; skipping persistence")
        return
    ensure_thread(tid)
    current = get_state(tid)
    if current is None:
        current = json.loads(json.dumps(DEFAULT_STATE))
    for k, v in patch.items():
        if isinstance(current.get(k), dict) and isinstance(v, dict):
            current[k] = {**current.get(k, {}), **v}
        else:
            current[k] = v
    _write_state_row(tid, json.dumps(current))


def save_state_full(thread_id: str, state: dict[str, Any]) -> None:
    """Replace state entirely (no merge). Use with ThreadState.to_dict()."""
    tid = (thread_id or "").strip()
    if not tid:
        logger.warning("save_state_full called with empty thread_id; skipping persistence")
        return
    ensure_thread(tid)
    _write_state_row(tid, json.dumps(state))


# -------------------------------------------------------------------
# Upload records
# -------------------------------------------------------------------


_MAX_THREAD_UPLOAD_RECORDS = 15


def append_uploaded_file_record(thread_id: str, record: dict[str, Any]) -> bool:
    """Prepend an upload record to active.uploaded_files (capped).

    Returns False if state could not be persisted (e.g. DB unavailable).
    """
    current = get_state(thread_id)
    if current is None:
        # Either no row yet, or DB unreachable. If DB is reachable we'll
        # create a fresh state; if not, save_state's connection_error
        # branch will log + no-op and we still return True to the caller
        # because the caller only uses the bool to decide whether to skip
        # optimistic UI. Keep pre-refactor behavior: return False only
        # when get_state couldn't connect (we can't distinguish cleanly
        # from "no row" without another probe). Historically this path
        # returned False when URL was unset — the agent's connection_error
        # surfaces via _err_code in the probe below.
        current = json.loads(json.dumps(DEFAULT_STATE))

    # Probe reachability: if we can't write, return False to match legacy.
    probe = db_query("SELECT 1 AS ok", _DB)
    if _is_connection_error(probe):
        logger.warning("db-agent unreachable; upload list not persisted")
        return False

    active = {**(current.get("active") or {})}
    prev = active.get("uploaded_files") or []
    files: list[dict[str, Any]] = [dict(x) for x in prev if isinstance(x, dict)]
    files.insert(0, dict(record))
    active["uploaded_files"] = files[:_MAX_THREAD_UPLOAD_RECORDS]
    save_state(thread_id, {"active": active})
    return True


def register_open_slots(thread_id: str, slots: list[str]) -> None:
    """Set state.open_slots to slots (replace), increment state_version, save."""
    from app.state.model import ThreadState

    raw = get_state(thread_id)
    thread_state = ThreadState.from_dict(raw)
    thread_state.apply_delta({"open_slots": list(slots) if slots else []})
    save_state_full(thread_id, thread_state.to_dict())
