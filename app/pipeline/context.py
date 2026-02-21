"""Pipeline context: correlation_id, thread_id, state, plan, and stage data.

Holds everything the pipeline stages need to read/write. Stages modify context
in place; no patch merge—explicit transitions via apply_delta on state.
"""
from dataclasses import dataclass, field
from typing import Any

from app.planner.schemas import Plan


@dataclass
class PipelineContext:
    """Context passed through the pipeline. Stages read and update fields in place."""

    correlation_id: str
    thread_id: str | None
    message: str
    """Raw user message for this turn."""

    # State (patch-based for now; will migrate to ThreadState in Phase 2)
    merged_state: dict[str, Any] = field(default_factory=dict)
    """Current thread state after load + apply patch."""
    last_turns: list[dict] = field(default_factory=list)
    """Last turn messages for context (user + assistant)."""
    last_turn_sources: list[dict] = field(default_factory=list)
    """Sources (document_id, document_name) from previous turns for continuity."""
    context_pack: str = ""
    """Context string passed to parser (from route_context + build_context_pack)."""

    # Classification
    classification: str = ""
    """slot_fill | new_question."""
    effective_message: str = ""
    """Message used for planning: either slot_fill merge or raw message."""

    # Plan
    plan: Plan | None = None
    """Parsed plan with subquestions. Built in plan stage, updated on refinement."""

    blueprint: list[dict] = field(default_factory=list)
    """Per-subquestion execution config (agent, sensitivity, rag_k, etc.)."""

    refined_query: str | None = None
    """Canonical question: from plan on new_question, merged on slot_fill."""

    # Clarification / refinement
    needs_clarification: bool = False
    clarification_message: str | None = None
    missing_slots: list[str] = field(default_factory=list)

    should_refine: bool = False
    refinement_suggestions: list[str] = field(default_factory=list)
    refinement_message: str | None = None

    # Resolution (filled in resolve stage)
    answers: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    usages: list[dict] = field(default_factory=list)
    retrieval_signals: list[str] = field(default_factory=list)

    # Integrate
    final_message: str = ""
    response_payload: dict[str, Any] | None = None

    # Collected during pipeline (emitter appends)
    thinking_chunks: list[str] = field(default_factory=list)

    def has_thread(self) -> bool:
        return bool(self.thread_id and (self.thread_id or "").strip())
