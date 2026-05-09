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

    system_context: str | None = None
    """Pre-loaded ground-truth context passed by the caller (POST /chat's
    ``system_context`` field). When set, ReAct runs a Round 0 attempt
    that tries to answer from this context alone before entering the
    tool loop. Primary consumer: story presentation layer node clicks
    where verified values are already known."""

    cache_assist_override: bool | None = None
    """Per-turn override for cache-assist from POST /chat. None = use
    orchestrator's normal mode-selection rules; False = force off;
    True = force on (subject to all other gates: agentic-mode,
    system_context, freshness markers still take precedence)."""

    cache_mode: str = "none"
    """Selected cache-assist mode for this turn: ``active`` (candidates
    shown to LLM), ``shadow`` (logged only), ``off`` (skipped), or
    ``none`` (feature off globally or not applicable)."""

    cache_candidates: list = field(default_factory=list)
    """Raw candidates returned by cached_answer_lookup (the skill's
    ``extra.candidates`` list). Populated on both active and shadow
    modes so shadow-log writes have the data they need."""

    cache_influence: str = "none"
    """How the cache influenced finalization: ``none``, ``partial``,
    ``verbatim``, ``rejected``, or ``unknown``. Stamped onto chat_turns."""

    seed_tool_results: list = field(default_factory=list)
    """Pre-populated ``tool_results`` entries that the ReAct loop
    should treat as already-executed before round 1 starts. Used by
    the cache-assist path to inject cached_answer_lookup output as a
    "round 0 virtual tool result" so the existing
    build_reasoning_context machinery picks it up without special
    casing. Empty when no pre-population applies."""

    user_id: str | None = None
    """Authenticated user_id from POST /chat's ``require_user`` dependency.

    Phase 2d added the dependency + 401 gating for hosted mode;
    2026-04-19 commit added the plumbing from POST /chat → worker →
    here → insert_turn so chat_turns rows get stamped. None in dev
    (``CHAT_AUTH_MODE=off``) or when auth middleware couldn't decode
    a JWT.
    """

    user_profile: dict[str, Any] | None = None
    """User profile from mobius-user (``GET /me``'s ``user.profile`` field).

    Carries ``rendered_prompt`` (4-6 line tailored system block) plus
    structured fields (``communication``, ``autonomy``, ``tasks``,
    ``preferred_name``, ``timezone``). 2026-05-06 added so the pipeline
    can splice the rendered_prompt into per-stage system prompts and
    read autonomy for tool-execution gating.

    None when the user is un-onboarded or the FE didn't send a profile
    payload — handled by ``app.pipeline.personalization`` helpers as a
    no-op (use base prompt only). Travels: FE ChatRequest.profile →
    payload → worker → run_pipeline(user_profile=...) → here.
    """

    # State (patch-based for now; will migrate to ThreadState in Phase 2)
    merged_state: dict[str, Any] = field(default_factory=dict)
    """Current thread state after load + apply patch."""
    last_turns: list[dict] = field(default_factory=list)
    """Last turn messages for context (user + assistant)."""
    last_turn_sources: list[dict] = field(default_factory=list)
    """Sources (document_id, document_name) from previous turns for continuity."""
    context_pack: str = ""
    """Context string passed to parser (from route_context + build_context_pack)."""
    previous_thread_summary: str | None = None
    """Phase 13.7 — rolling thread summary from the most recent prior turn.
    Loaded by state_load from chat_turns.context_summary (latest non-null
    row in last_turns). Threaded into the integrator so it can REFINE
    the summary to integrate THIS turn rather than starting fresh."""
    thread_summary: str | None = None
    """Phase 13.7 — refined rolling summary produced by THIS turn's
    integrator. Persisted to chat_turns.context_summary in
    _atomic_save_turn_with_messages. Sidebar reads the latest non-null
    value across a thread's turns."""

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
    react_rounds_used: int = 0
    """Actual ReAct loop round count — set by run_react when it exits.

    2026-04-19 (Sprint A.1 commit 4 follow-up): feeds the turn_completed
    envelope's rounds_used field for accurate throughput analytics.
    Previously _publish_completed used len(thinking_chunks) as a proxy,
    which over-counted by 5-10× because every emit contributes an
    entry. This field is set exactly once per turn to the number of
    ReAct rounds that actually ran (1..max_it)."""
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

    # Inbound credentialing options from POST /chat (parallel feature branch;
    # restored here so the orchestrator's run_pipeline(..., credentialing_options=...)
    # signature doesn't TypeError on every turn).
    credentialing_options: dict[str, Any] | None = None

    # UI chat mode (POST /chat chat_mode): copilot | agentic | quick | task
    chat_mode: str = "copilot"

    # Tool policy: resolved in orchestrator from mode default + user subscriptions.
    # None  = no filter (all tools visible to the planner).
    # []    = no tools (task mode default, or user disabled everything).
    # [..] = explicit allow-list passed to get_tool_manifest(allowed=...).
    allowed_tools: list[str] | None = None

    # Set True when quick mode hits max rounds or produces a long answer — client shows "Full answer" link
    quick_truncated: bool = False

    # Post-run adjudication (integrator llm_calls row for quality_score attachment)
    integrator_llm_call_id: str | None = None
    integrator_model_id: str | None = None

    def has_thread(self) -> bool:
        return bool(self.thread_id and (self.thread_id or "").strip())
