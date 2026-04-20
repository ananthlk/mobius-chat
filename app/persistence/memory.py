"""Session-scoped in-memory persistence: used when DB is not configured.

Keeps state and turns for the lifetime of a worker process with explicit TTL.
Emits a startup warning so operators know persistence is not durable.
"""
import logging
import time
from typing import Any

from app.persistence.interface import PersistencePort

logger = logging.getLogger(__name__)

SESSION_TTL = 1800  # 30 minutes

# Module-level store — survives within a single worker process.
# Key -> (data, expires_at)
_store: dict[str, tuple[Any, float]] = {}


def _set(key: str, value: Any) -> None:
    _store[key] = (value, time.time() + SESSION_TTL)
    _evict()


def _get(key: str) -> Any | None:
    entry = _store.get(key)
    if not entry:
        return None
    data, expires_at = entry
    if time.time() > expires_at:
        del _store[key]
        return None
    return data


def _evict() -> None:
    now = time.time()
    expired = [k for k, (_, exp) in list(_store.items()) if now > exp]
    for k in expired:
        _store.pop(k, None)


class MemoryPersistence(PersistencePort):
    """Session-scoped in-memory fallback. State survives within one process, lost on restart."""

    def __init__(self) -> None:
        logger.warning(
            "MemoryPersistence active: thread state is in-memory only. "
            "Set CHAT_RAG_DATABASE_URL for durable persistence."
        )

    # --- PersistencePort implementation ---

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
        user_id: str | None = None,  # Phase 2d: accepted for signature parity; in-memory backend doesn't persist  # noqa: ARG002
    ) -> None:
        if not thread_id:
            return
        key = f"turns:{thread_id}"
        turns: list[dict[str, Any]] = _get(key) or []
        turns.append({
            "turn_id": correlation_id,
            "user_content": question,
            "assistant_content": final_message,
            "created_at": time.time(),
        })
        _set(key, turns[-10:])  # keep last 10 turns in memory

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
        self.save_turn(
            correlation_id, question, thinking_log, final_message, sources,
            duration_ms, model_used, llm_provider,
            thread_id=thread_id, plan_snapshot=plan_snapshot,
            source_confidence_strip=source_confidence_strip, config_sha=config_sha,
            user_id=user_id,
        )
        if thread_id:
            self.append_messages(thread_id, correlation_id, user_content, assistant_content)

    def append_messages(
        self,
        thread_id: str,
        turn_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        key = f"turns:{thread_id}"
        turns: list[dict[str, Any]] = _get(key) or []
        # Upsert by turn_id
        existing = next((t for t in turns if t.get("turn_id") == turn_id), None)
        if existing:
            existing["user_content"] = user_content
            existing["assistant_content"] = assistant_content
        else:
            turns.append({
                "turn_id": turn_id,
                "user_content": user_content,
                "assistant_content": assistant_content,
                "created_at": time.time(),
            })
        _set(key, turns[-10:])

    def save_state(self, thread_id: str, state: dict[str, Any]) -> None:
        _set(f"state:{thread_id}", state)

    def load_state(self, thread_id: str) -> dict[str, Any] | None:
        return _get(f"state:{thread_id}")

    def get_last_turns(self, thread_id: str, n: int = 2) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = _get(f"turns:{thread_id}") or []
        return turns[-n:]

    def append_progress_event(self, correlation_id: str, event_type: str, event_data: dict[str, Any]) -> None:
        pass  # progress events are not needed in no-DB mode
