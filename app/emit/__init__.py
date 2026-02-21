"""Structured emits: EmitEvent, Emitter, adapter for technical→user mapping."""

from app.emit.types import EmitEvent, EmitLevel
from app.emit.emitter import PipelineEmitter, create_pipeline_emitter

__all__ = ["EmitEvent", "EmitLevel", "PipelineEmitter", "create_pipeline_emitter"]
