"""In-memory persistence: explicit no-DB mode. Logs and skips—no silent degradation."""
import logging
from typing import Any

from app.persistence.interface import PersistencePort

logger = logging.getLogger(__name__)


class MemoryPersistence(PersistencePort):
    """Explicit no-DB mode. All operations log and no-op."""

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
        logger.info("[persistence] no DB: save_turn_with_messages skipped for %s", correlation_id[:8])

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
        logger.info("[persistence] no DB: save_turn skipped for %s", correlation_id[:8])

    def append_messages(
        self,
        thread_id: str,
        turn_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        logger.info("[persistence] no DB: append_messages skipped for thread %s", thread_id[:8])

    def save_state(self, thread_id: str, state: dict[str, Any]) -> None:
        logger.info("[persistence] no DB: save_state skipped for thread %s", thread_id[:8])

    def append_progress_event(self, correlation_id: str, event_type: str, event_data: dict[str, Any]) -> None:
        pass
