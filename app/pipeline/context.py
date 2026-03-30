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
    # Server-authored choice groups merged into response ``clarification_options`` (NPI pick, future workflows).
    pending_workflow_selection: list[dict[str, Any]] = field(default_factory=list)

    needs_route_clarification: bool = False
    """Route clash: multiple conflicting deterministic triggers (web vs RAG)."""
    route_clarification_choices: list[dict] = field(default_factory=list)

    should_refine: bool = False
    refinement_suggestions: list[str] = field(default_factory=list)
    refinement_message: str | None = None

    # Resolution (filled in resolve stage)
    answers: list[str] = field(default_factory=list)
    answer_set: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Per sq_id: {answer, source, status}. Resolver, user context, and integrator can update."""
    sources: list[dict] = field(default_factory=list)
    usages: list[dict] = field(default_factory=list)
    retrieval_signals: list[str] = field(default_factory=list)

    # Integrate
    final_message: str = ""
    response_payload: dict[str, Any] | None = None

    # Conversational continuity: last turn failed (no_sources / layer 5) for next-turn resolver
    failed_query: dict[str, Any] | None = None

    # Active skill context: last skill run (roster_report / npi_lookup) for follow-up questions
    active_skill: dict[str, Any] | None = None
    active_skill_reference: bool = False
    """True when current message refers to active_skill output → answer from context, not RAG/web."""
    active_skill_name: str | None = None

    # ReAct: active context from last tool (replaces active_skill when using run_react)
    active_context: dict[str, Any] | None = None
    """Tool output for follow-up questions (tool, org, summary, follow_up_capable, expires_after_turns)."""

    react_last_tool: str | None = None
    """Last tool name from ReAct loop (UI attribution / assistant_envelope)."""

    # Collected during pipeline (emitter appends)
    thinking_chunks: list[str] = field(default_factory=list)

    # Relentless continuity: master objective (created after plan, updated after resolve)
    master_objective: dict | None = None
    # User-provided context (when user shares docs/links/info to help answer)
    user_provided_context: str | None = None
    # Roster/credentialing: step outputs (CSV per step) for validation UI
    roster_step_outputs: list[dict] | None = None
    # Roster/credentialing: report run id (for persistence / fetch run)
    report_run_id: str | None = None
    # Roster/credentialing: report PDF as base64 for download
    roster_report_pdf_base64: str | None = None
    # Roster/credentialing: final report markdown for download when PDF unavailable
    roster_report_final_md: str | None = None
    # Download filenames: "reconciliation" (roster upload vs outside-in) vs "credentialing" (11-step waterfall)
    roster_report_attachments_kind: str | None = None
    # Co-pilot credentialing: run_id, pending_step_id, draft_output for validation UI
    credentialing_copilot: dict[str, Any] | None = None

    # Client envelope (POST /chat credentialing_options): merged into run_credentialing_report
    credentialing_options: dict[str, Any] | None = None

    # UI chat mode (POST /chat chat_mode): copilot | agentic | quick
    chat_mode: str = "copilot"

    # Set True when quick mode hits max rounds or produces a long answer — client shows "Full answer" link
    quick_truncated: bool = False

    # Post-run adjudication (integrator llm_calls row for quality_score attachment)
    integrator_llm_call_id: str | None = None
    integrator_model_id: str | None = None

    def has_thread(self) -> bool:
        return bool(self.thread_id and (self.thread_id or "").strip())
