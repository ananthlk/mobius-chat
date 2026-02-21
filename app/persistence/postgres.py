"""Postgres persistence: wraps current storage (turns, threads)."""
import json
import logging
from typing import Any

from app.persistence.interface import PersistencePort
from app.storage.threads import append_turn_messages, save_state_full
from app.storage.turns import insert_turn

logger = logging.getLogger(__name__)


def _get_db_url() -> str:
    from app.chat_config import get_chat_config
    return (get_chat_config().rag.database_url or "").strip()


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
) -> None:
    """Single transaction: insert turn + append user/assistant messages."""
    import psycopg2

    url = _get_db_url()
    if not url:
        logger.warning("CHAT_RAG_DATABASE_URL not set; turn not persisted")
        return
    conn = psycopg2.connect(url)
    try:
        cur = conn.cursor()
        thread_val = (thread_id or "").strip() or None
        strip_val = (source_confidence_strip or "").strip() or None
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
                None,
                thread_val,
                json.dumps(plan_snapshot) if plan_snapshot is not None else None,
                None,
                None,
                strip_val,
                config_sha_val,
            ),
        )
        if thread_id and thread_val:
            u = (user_content or "").strip() or ""
            a = (assistant_content or "").strip() or ""
            cur.execute(
                """
                INSERT INTO chat_turn_messages (turn_id, thread_id, role, content, created_at)
                VALUES (%s, %s, 'user', %s, now())
                ON CONFLICT (turn_id, role) DO UPDATE SET content = EXCLUDED.content, created_at = now()
                """,
                (correlation_id, thread_val, u),
            )
            cur.execute(
                """
                INSERT INTO chat_turn_messages (turn_id, thread_id, role, content, created_at)
                VALUES (%s, %s, 'assistant', %s, now())
                ON CONFLICT (turn_id, role) DO UPDATE SET content = EXCLUDED.content, created_at = now()
                """,
                (correlation_id, thread_val, a),
            )
        conn.commit()
    finally:
        conn.close()


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
    ) -> None:
        """Atomic: turn + messages in one transaction."""
        _atomic_save_turn_with_messages(
            correlation_id, question, thinking_log, final_message, sources,
            duration_ms, model_used, llm_provider, thread_id,
            user_content, assistant_content,
            plan_snapshot, source_confidence_strip, config_sha,
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
        url = _get_db_url()
        if not url:
            return
        try:
            import psycopg2
            line = event_data.get("line", "") or event_data.get("message", "")
            chunk = event_data.get("chunk", "") or event_data.get("message", "")
            if event_type == "thinking" and line:
                ev_data = {"line": line}
            elif event_type == "message" and chunk:
                ev_data = {"chunk": chunk}
            else:
                ev_data = event_data
            conn = psycopg2.connect(url)
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO chat_progress_events (correlation_id, event_type, event_data) VALUES (%s, %s, %s)",
                    (correlation_id, event_type, json.dumps(ev_data)),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.debug("append_progress_event: %s", e)
