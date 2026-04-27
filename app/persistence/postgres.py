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


#
# Schema dependencies (run order matters):
#   * 017_chat_turns_context_summary.sql — adds context_summary TEXT
#   * 032_chat_turns_user_id.sql         — adds user_id TEXT
#   * 033_phase_13_7_thread_summary.sql  — adds the (thread_id, created_at DESC)
#       partial index used by the sidebar's latest-summary query
#
# Both columns ARE on dev/prod. The two-tier fallback below (drop
# user_id; drop user_id+context_summary) is purely defensive against
# environments that lag on migrations — in normal operation it never
# fires. If you see fallback warnings in Cloud Run logs, treat that as
# a schema-drift incident, not a routine event.
#
_TURN_INSERT_SQL_WITH_USER_ID = """
INSERT INTO chat_turns (
    correlation_id, question, thinking_log, final_message, sources,
    duration_ms, model_used, llm_provider, session_id, thread_id,
    plan_snapshot, blueprint_snapshot, agent_cards, source_confidence_strip, config_sha,
    context_summary, user_id
)
VALUES (
    :correlation_id, :question, :thinking_log, :final_message, :sources,
    :duration_ms, :model_used, :llm_provider, :session_id, :thread_id,
    :plan_snapshot, :blueprint_snapshot, :agent_cards, :source_confidence_strip, :config_sha,
    :context_summary, :user_id
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
    context_summary = COALESCE(EXCLUDED.context_summary, chat_turns.context_summary),
    user_id = COALESCE(EXCLUDED.user_id, chat_turns.user_id)
"""

_TURN_INSERT_SQL_NO_USER_ID = """
INSERT INTO chat_turns (
    correlation_id, question, thinking_log, final_message, sources,
    duration_ms, model_used, llm_provider, session_id, thread_id,
    plan_snapshot, blueprint_snapshot, agent_cards, source_confidence_strip, config_sha,
    context_summary
)
VALUES (
    :correlation_id, :question, :thinking_log, :final_message, :sources,
    :duration_ms, :model_used, :llm_provider, :session_id, :thread_id,
    :plan_snapshot, :blueprint_snapshot, :agent_cards, :source_confidence_strip, :config_sha,
    :context_summary
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
    context_summary = COALESCE(EXCLUDED.context_summary, chat_turns.context_summary)
"""

# Fallback used when chat_turns has neither user_id NOR context_summary
# columns (older schemas). Mirrors the historical behavior before
# Phase 13.7 added the context_summary write to this code path. The
# turns.py path already handles its own column-missing fallback for
# the no-thread save flow; this one covers the thread-with-messages
# flow.
_TURN_INSERT_SQL_LEGACY_NO_CONTEXT = """
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
    context_summary: str | None = None,
) -> None:
    """Single transaction: insert turn + append user/assistant messages.

    ``user_id`` (Phase 2d): authenticated user_id from ``require_user``.
    None in dev / no-auth mode. On hosts where the ``user_id`` column
    hasn't been added yet we retry with the non-user_id column list —
    same graceful fallback the pre-refactor code had.

    ``context_summary`` (Phase 13.7): rolling thread summary produced
    by the integrator. Persisted to ``chat_turns.context_summary`` so
    the sidebar can show a per-thread tldr that morphs across turns
    AND so the next turn's state_load can pull it back in for refine
    (vs. rebuild). Single-shot (no-thread) saves go through
    ``insert_turn`` in storage/turns.py which has its own context_
    summary write path (regex-based heuristic) — this thread-with-
    messages path now mirrors that behavior, but with the LLM-built
    summary instead of the regex one.
    """
    thread_val = (thread_id or "").strip() or None
    strip_val = (source_confidence_strip or "").strip() or None
    config_sha_val = (config_sha or "").strip() or None
    user_id_val = (user_id or "").strip() or None
    context_summary_val = (context_summary or "").strip() or None

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
        "context_summary": context_summary_val,
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
        # BETA-sprint Move 3 — record happy-path so dashboard ratios
        # work (fallback rate = tier_1+2 / total). Fire-and-forget.
        try:
            from app.services.phase_13_7_metrics import record_persist_fallback_tier
            record_persist_fallback_tier(0)
        except Exception:
            pass
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

    if code == "column_missing" or "user_id" in msg_lower or "context_summary" in msg_lower or (
        "column" in msg_lower and "does not exist" in msg_lower
    ):
        # First fallback: drop user_id, keep context_summary (covers the
        # most-common case: user_id column missing on older schemas).
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
            # BETA-sprint Move 3 — first-tier fallback fired (user_id
            # column missing). Schema-drift signal; alert on count > 0.
            try:
                from app.services.phase_13_7_metrics import record_persist_fallback_tier
                record_persist_fallback_tier(1)
            except Exception:
                pass
            return

        # Second fallback: drop context_summary too. Covers older
        # schemas where neither column is present. Phase 13.7's
        # context_summary column has been on dev's schema for a while
        # (used by the regex-based insert_turn path), but be defensive.
        msg2 = (err2.get("message") if isinstance(err2, dict) else str(err2)) or ""
        if "context_summary" in msg2.lower() or "column" in msg2.lower():
            legacy_params = {
                k: v for k, v in turn_params_full.items()
                if k not in ("user_id", "context_summary")
            }
            legacy_statements = [
                {"sql": _TURN_INSERT_SQL_LEGACY_NO_CONTEXT, "params": legacy_params}
            ]
            if thread_id and thread_val:
                legacy_statements.append(statements[1])
                legacy_statements.append(statements[2])
            result3 = db_transaction(legacy_statements, "chat")
            err3 = result3.get("error") if isinstance(result3, dict) else None
            if err3 is None:
                # BETA-sprint Move 3 — second-tier fallback fired (BOTH
                # user_id and context_summary columns missing). This is
                # a serious schema-drift incident — the rolling summary
                # write is silently dropped on this turn. Alert hard.
                try:
                    from app.services.phase_13_7_metrics import record_persist_fallback_tier
                    record_persist_fallback_tier(2)
                except Exception:
                    pass
                return
            msg3 = err3.get("message") if isinstance(err3, dict) else str(err3)
            logger.exception("Atomic turn save (legacy-no-context fallback) failed: %s", msg3)
            raise RuntimeError(msg3)

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
        context_summary: str | None = None,
    ) -> None:
        """Atomic: turn + messages in one transaction.

        ``context_summary`` is the rolling thread summary produced by
        the integrator (Phase 13.7). Optional — None falls back to
        existing behavior (no summary stamped, sidebar will use the
        first-turn question as title).
        """
        _atomic_save_turn_with_messages(
            correlation_id, question, thinking_log, final_message, sources,
            duration_ms, model_used, llm_provider, thread_id,
            user_content, assistant_content,
            plan_snapshot, source_confidence_strip, config_sha,
            user_id,
            context_summary=context_summary,
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
