"""Emitter interface: emit structured events."""
from collections.abc import Callable
from typing import Any

from app.emit.types import EmitEvent
from app.emit.adapter import wrap_technical_for_user


class PipelineEmitter:
    """Emitter that forwards to on_thinking. Uses adapter for retrieval-stage messages."""

    def __init__(
        self,
        correlation_id: str,
        on_thinking: Callable[[str], None],
        stage: str = "pipeline",
        user_friendly: bool = True,
    ) -> None:
        self.correlation_id = correlation_id
        self._on_thinking = on_thinking
        self._stage = stage
        self._user_friendly = user_friendly

    def emit(self, event: EmitEvent) -> None:
        msg = (event.message or "").strip()
        if not msg:
            return
        if event.stage == "retrieve" or "retrieval" in (event.stage or "").lower():
            mapped = wrap_technical_for_user(msg, user_friendly=self._user_friendly)
            if mapped:
                self._on_thinking(mapped)
        else:
            self._on_thinking(msg)


def create_pipeline_emitter(
    correlation_id: str,
    on_thinking: Callable[[str], None],
    stage: str = "pipeline",
) -> PipelineEmitter:
    """Create emitter for pipeline use."""
    return PipelineEmitter(correlation_id, on_thinking, stage=stage, user_friendly=True)
