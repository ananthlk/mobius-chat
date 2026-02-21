"""Emit event types for pipeline stages."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

EmitLevel = Literal["technical", "user", "both"]


@dataclass
class EmitEvent:
    """Structured emit: stage, message, level, timestamp."""

    correlation_id: str
    stage: str
    message: str
    level: EmitLevel = "both"
    ts: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ts is None:
            self.ts = datetime.utcnow()
