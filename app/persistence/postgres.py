"""Postgres persistence: wraps current storage (turns, threads).

db-agent refactor (2026-04-19)
------------------------------
All DB access now routes through ``app.db_client`` → mobius-db-agent.

- ``append_progress_event``: single-statement write, uses ``db_execute``.
- ``_atomic_save_turn_with_messages``: three writes (turn + user msg +
  assistant msg) in one transaction, uses ``db_transaction``. A mid-
  sequence failure rolls back so we never end up with a chat_turns row
  that has no paired chat_turn_messages — the original atomicity
  invariant, now enforced via the agent.

Graceful fallback for missing ``user_id`` column on chat_turns: we try
the full INSERT first; on ``column_missing`` (or text match for older
drivers) we retry via db_transaction with the non-user_id column list.
Same shape the pre-refactor code had, just through the agent.
"""
import json
import logging
from typing import Any

from app.db_client import db_execute, db_transaction
from app.persistence.interface import PersistencePort
from app.storage.threads import append_turn_messages, save_state_full
from app.storage.turns import insert_turn

logger = logging.getLogger(__name__)


_TURN_INSERT_SQL_WITH_USER_ID = """
INSERT INTO chat_turns (
    correlation_id, question, thinking_log, final_message, sources,
    duration_ms, model_used, llm_provider, session_id, thread_id,
    plan_snapshot, blueprint_snapshot, agent_cards, source_confidence_strip, config_sha,
    user_id
)
VALUES (
    :correlation_id, :question, :thinking_log, :final_message, :sources,
    :duration_ms, :model_used, :llm_provider, :session_id, :thread_id,
    :plan_snapshot, :blueprint_snapshot, :agent_cards, :source_confidence_strip, :config_sha,
    :user_id
)
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
    config_sha = EXCLUDED.config_sha,
    user_id = COALESCE(EXCLUDED.user_id, chat_turns.user_id)
"""

_TURN_INSERT_SQL_NO_USER_ID = """
INSERT INTO chat_turns (
    correlation_id, question, thinking_log, final_message, sources,
    duration_ms, model_used, llm_provider, session_id, thread_id,
    plan_snapshot, blueprint_snapshot, agent_cards, source_confidence_strip, config_sha
)
VALUES (
    :correlation_id, :question, :thinking_log, :final_message, :sources,
    :duration_ms, :model_used, :llm_provider, :session_id, :thread_id,
    :plan_snapshot, :blueprint_snapshot, :agent_cards, :source_confidence_strip, :config_sha
)
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
"""

_MESSAGE_INSERT_SQL = """
INSERT INTO chat_turn_messages (turn_id, thread_id, role, content, created_at)
VALUES (:turn_id, :thread_id, :role, :content, now())
ON CONFLICT (turn_id, role) DO UPDATE SET content = EXCLUDED.content, created_at = now()
"""


def _atomic_save_turn_with_messages(
    correlation_id: str,
    question: str,
    thinking_log: list[str],
    final_message: str,
    sources: list[dict[str, Any]],
    duration_ms: int | None,
    model_used: str | None,
    llm_provider: str | None,
    thread_id: str | None,
    user_content: str,
    assistant_content: str,
    plan_snapshot: dict[str, Any] | None,
    source_confidence_strip: str | None,
    config_sha: str | None,
    user_id: str | None = None,
) -> None:
    """Single transaction: insert turn + append user/assistant messages.

    ``user_id`` (Phase 2d): authenticated user_id from ``require_user``.
    None in dev / no-auth mode. On hosts where the ``user_id`` column
    hasn't been added yet we retry with the non-user_id column list —
    same graceful fallback the pre-refactor code had.
    """
    thread_val = (thread_id or "").strip() or None
    strip_val = (source_confidence_strip or "").strip() or None
    config_sha_val = (config_sha or "").strip() or None
    user_id_val = (user_id or "").strip() or None

    turn_params_full = {
        "correlation_id": correlation_id,
        "question": (question or "").strip() or "",
        "thinking_log": json.dumps(thinking_log or []),
        "final_message": (final_message or "").strip() or None,
        "sources": json.dumps(sources or []),
        "duration_ms": duration_ms,
        "model_used": (model_used or "").strip() or None,
        "llm_provider": (llm_provider or "").strip() or None,
        "session_id": None,
        "thread_id": thread_val,
        "plan_snapshot": json.dumps(plan_snapshot) if plan_snapshot is not None else None,
        "blueprint_snapshot": None,
        "agent_cards": None,
        "source_confidence_strip": strip_val,
        "config_sha": config_sha_val,
        "user_id": user_id_val,
    }

    statements: list[dict[str, Any]] = [
        {"sql": _TURN_INSERT_SQL_WITH_USER_ID, "params": turn_params_full}
    ]
    if thread_id and thread_val:
        u = (user_content or "").strip() or ""
        a = (assistant_content or "").strip() or ""
        statements.append({
            "sql": _MESSAGE_INSERT_SQL,
            "params": {"turn_id": correlation_id, "thread_id": thread_val,
                       "role": "user", "content": u},
        })
        statements.append({
            "sql": _MESSAGE_INSERT_SQL,
            "params": {"turn_id": correlation_id, "thread_id": thread_val,
                       "role": "assistant", "content": a},
        })

    result = db_transaction(statements, "chat")
    err = result.get("error") if isinstance(result, dict) else None
    if err is None:
        return

    # Graceful fallback — missing user_id column (migration not yet run).
    # Same text-match heuristics the original code used, now augmented
    # with the structured error code.
    code = err.get("code") if isinstance(err, dict) else None
    msg = (err.get("message") if isinstance(err, dict) else str(err)) or ""
    msg_lower = msg.lower()

    if code == "connection_error":
        logger.warning("db-agent unreachable (or CHAT_RAG_DATABASE_URL unset); turn not persisted: %s", msg)
        return

    if code == "column_missing" or "user_id" in msg_lower or (
        "column" in msg_lower and "does not exist" in msg_lower
    ):
        fallback_turn_params = {k: v for k, v in turn_params_full.items() if k != "user_id"}
        fallback_statements = [
            {"sql": _TURN_INSERT_SQL_NO_USER_ID, "params": fallback_turn_params}
        ]
        if thread_id and thread_val:
            fallback_statements.append(statements[1])
            fallback_statements.append(statements[2])
        result2 = db_transaction(fallback_statements, "chat")
        err2 = result2.get("error") if isinstance(result2, dict) else None
        if err2 is None:
            return
        msg2 = err2.get("message") if isinstance(err2, dict) else str(err2)
        logger.exception("Atomic turn save (fallback) failed: %s", msg2)
        raise RuntimeError(msg2)

    logger.exception("Atomic turn save failed: %s", msg)
    raise RuntimeError(msg)


class PostgresPersistence(PersistencePort):
    """Postgres implementation. Uses existing storage modules. save_state still uses patch merge for now (Phase 2 will fix)."""

    def save_turn_with_messages(
        self,
        correlation_id: str,
        question: str,
        thinking_log: list[str],
        final_message: str,
        sources: list[dict[str, Any]],
        duration_ms: int | None,
        model_used: str | None,
        llm_provider: str | None,
        thread_id: str | None,
        user_content: str,
        assistant_content: str,
        *,
        plan_snapshot: dict[str, Any] | None = None,
        source_confidence_strip: str | None = None,
        config_sha: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """Atomic: turn + messages in one transaction."""
        _atomic_save_turn_with_messages(
            correlation_id, question, thinking_log, final_message, sources,
            duration_ms, model_used, llm_provider, thread_id,
            user_content, assistant_content,
            plan_snapshot, source_confidence_strip, config_sha,
            user_id,
        )

    def save_turn(
        self,
        correlation_id: str,
        question: str,
        thinking_log: list[str],
        final_message: str,
        sources: list[dict[str, Any]],
        duration_ms: int | None,
        model_used: str | None,
        llm_provider: str | None,
        *,
        session_id: str | None = None,
        thread_id: str | None = None,
        plan_snapshot: dict[str, Any] | None = None,
        source_confidence_strip: str | None = None,
        config_sha: str | None = None,
        user_id: str | None = None,
    ) -> None:
        insert_turn(
            correlation_id=correlation_id,
            question=question,
            thinking_log=thinking_log,
            final_message=final_message,
            sources=sources,
            duration_ms=duration_ms,
            model_used=model_used,
            llm_provider=llm_provider,
            session_id=session_id,
            thread_id=thread_id,
            plan_snapshot=plan_snapshot,
            source_confidence_strip=source_confidence_strip,
            config_sha=config_sha,
            user_id=user_id,
        )

    def append_messages(
        self,
        thread_id: str,
        turn_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        append_turn_messages(thread_id, turn_id, user_content, assistant_content)

    def save_state(self, thread_id: str, state: dict[str, Any]) -> None:
        """Full state replace."""
        save_state_full(thread_id, state)

    def append_progress_event(self, correlation_id: str, event_type: str, event_data: dict[str, Any]) -> None:
        """Persist event to chat_progress_events. Used by progress module for DB write."""
        line = event_data.get("line", "") or event_data.get("message", "")
        chunk = event_data.get("chunk", "") or event_data.get("message", "")
        if event_type == "thinking" and line:
            ev_data = {"line": line}
        elif event_type == "message" and chunk:
            ev_data = {"chunk": chunk}
        else:
            ev_data = event_data
        result = db_execute(
            "INSERT INTO chat_progress_events (correlation_id, event_type, event_data) "
            "VALUES (:cid, :ev_type, CAST(:ev_data AS jsonb))",
            "chat",
            params={
                "cid": correlation_id,
                "ev_type": event_type,
                "ev_data": json.dumps(ev_data),
            },
        )
        err = result.get("error") if isinstance(result, dict) else None
        if err:
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            logger.debug("append_progress_event: %s", msg)
