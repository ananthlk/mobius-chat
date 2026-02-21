"""Persistence port: single interface for turns, messages, state, progress."""
from abc import ABC, abstractmethod
from typing import Any


class PersistencePort(ABC):
    """Abstract interface for persistence."""

    @abstractmethod
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
        session_id: str | None = None,
        thread_id: str | None = None,
        plan_snapshot: dict[str, Any] | None = None,
        source_confidence_strip: str | None = None,
        config_sha: str | None = None,
    ) -> None:
        pass

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
        """Atomic when possible: save turn + append messages. Default: separate calls."""
        self.save_turn(
            correlation_id, question, thinking_log, final_message, sources,
            duration_ms, model_used, llm_provider,
            thread_id=thread_id, plan_snapshot=plan_snapshot,
            source_confidence_strip=source_confidence_strip, config_sha=config_sha,
        )
        if thread_id:
            self.append_messages(thread_id, correlation_id, user_content, assistant_content)

    @abstractmethod
    def append_messages(
        self,
        thread_id: str,
        turn_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        pass

    @abstractmethod
    def save_state(self, thread_id: str, state: dict[str, Any]) -> None:
        pass

    @abstractmethod
    def append_progress_event(self, correlation_id: str, event_type: str, event_data: dict[str, Any]) -> None:
        pass
