"""Pydantic models for the planner: subquestions, plan, and Mobius TaskPlan schema."""
from pydantic import BaseModel, Field
from typing import Any, Literal

QuestionIntent = Literal["factual", "canonical"]
QuestionIntentExtended = Literal["factual", "canonical", "procedural", "diagnostic", "creative"]
Modality = Literal["rag", "tools", "web", "reasoning", "ask_user", "refuse", "synthesize"]
JurisdictionField = Literal[
    "state", "payer", "program", "timeframe", "plan", "population", "setting", "provider_type"
]


class SubQuestion(BaseModel):
    """One subquestion from decomposition, with patient vs non-patient and question intent."""
    id: str = Field(..., description="Unique id for this subquestion (e.g. sq1, sq2)")
    text: str = Field(..., description="The subquestion text")
    kind: Literal["patient", "non_patient", "tool"] = Field(
        ...,
        description="patient = we do not have access; non_patient = RAG path; tool = external tool (TBD)",
    )
    question_intent: QuestionIntent | None = Field(
        default=None,
        description="factual = specific fact/lookup; canonical = policy/process description. Used to prioritize RAG retrieval.",
    )
    intent_score: float | None = Field(
        default=None,
        description="Numeric blend in [0, 1]: 0 = canonical (hierarchical retrieval), 1 = factual. Drives mix of hierarchical vs factual retrieval.",
    )
    requires_jurisdiction: bool | None = Field(
        default=None,
        description="True if this subquestion needs payer/state scope. False for meta, general how-to. None = use legacy heuristics.",
    )
    on_rag_fail: list[str] = Field(
        default_factory=list,
        description="Fallback when RAG returns no/weak sources. E.g. ['search_google'] = search + scrape + pass as RAG.",
    )
    capabilities_primary: str | None = Field(
        default=None,
        description="Primary modality from planner: rag, tools, web, reasoning, ask_user, refuse. Drives agent routing.",
    )


class Plan(BaseModel):
    """Output of the planner: list of subquestions and optional thinking log. Legacy format used by pipeline."""
    subquestions: list[SubQuestion] = Field(default_factory=list)
    thinking_log: list[str] = Field(
        default_factory=list,
        description="Chunks emitted during planning (for 'thinking' display)",
    )
    llm_usage: dict[str, Any] | None = Field(
        default=None,
        description="LLM usage for planning call (provider, model, input_tokens, output_tokens) for billing.",
    )
    task_plan: "TaskPlan | None" = Field(
        default=None,
        description="Full Mobius TaskPlan when using new planner schema; None when using legacy format.",
    )


# --- Mobius TaskPlan schema (new orchestrator-friendly format) ---


class JurisdictionInfo(BaseModel):
    """Jurisdiction requirements for a subquestion. No values extracted; only metadata."""
    needed: bool = False
    required_fields: list[str] = Field(default_factory=list)
    blocking_if_missing: list[str] = Field(default_factory=list)
    can_default: list[str] = Field(default_factory=list)
    notes: str = ""


class CapabilitiesNeeded(BaseModel):
    """Primary modality and fallbacks for answering."""
    primary: Literal["rag", "tools", "web", "reasoning", "ask_user", "refuse"] = "rag"
    fallbacks: list[Literal["rag", "tools", "web", "reasoning", "ask_user", "refuse"]] = Field(
        default_factory=list
    )


class TaskPlanSubQuestion(BaseModel):
    """Subquestion in the new TaskPlan schema."""
    id: str
    text: str
    kind: Literal["patient", "non_patient", "tool"]
    question_intent: QuestionIntentExtended = "factual"
    intent_score: float = 0.5
    jurisdiction: JurisdictionInfo = Field(default_factory=JurisdictionInfo)
    capabilities_needed: CapabilitiesNeeded = Field(default_factory=CapabilitiesNeeded)


class ClarificationItem(BaseModel):
    """Clarification to ask the user when blocking fields are missing."""
    id: str
    subquestion_id: str
    question: str
    why_needed: str = ""
    blocking: bool = True
    fills: list[str] = Field(default_factory=list)


class TaskFallback(BaseModel):
    """Fallback action when a condition occurs. LLM outputs 'if' and 'then' keys."""
    if_condition: str = Field(default="", validation_alias="if")
    then: str = ""


class TaskInputs(BaseModel):
    """Inputs for a task (rag scopes, tools, web, jurisdiction)."""
    rag_scopes: list[str] = Field(default_factory=list)
    tool_capabilities: list[str] = Field(default_factory=list)
    web: dict[str, Any] = Field(default_factory=lambda: {"allowed": False})
    jurisdiction_fields_expected: list[str] = Field(default_factory=list)


class TaskStep(BaseModel):
    """Single step in a task."""
    step: int
    action: str


class OutputContract(BaseModel):
    """Expected output shape for a task."""
    type: Literal["answer", "bullets", "table", "json"] = "answer"
    must_include: list[str] = Field(default_factory=list)
    must_avoid: list[str] = Field(default_factory=list)


class TaskItem(BaseModel):
    """One executable task in the plan."""
    id: str
    subquestion_id: str
    modality: Modality = "rag"
    goal: str = ""
    inputs: TaskInputs = Field(default_factory=TaskInputs)
    steps: list[TaskStep] = Field(default_factory=list)
    fallbacks: list[TaskFallback] = Field(default_factory=list)
    output_contract: OutputContract = Field(default_factory=OutputContract)


class RetryPolicy(BaseModel):
    """Retry and failure handling."""
    max_attempts: int = 2
    on_missing_jurisdiction: str = "ask_blocking_clarification"
    on_no_results: str = "broaden_scope_then_offer_alternatives"
    on_tool_error: str = "simplify_then_fail_gracefully"


class SafetyInfo(BaseModel):
    """Safety metadata."""
    contains_patient_request: bool = False
    phi_risk: Literal["low", "medium", "high"] = "low"
    refusal_needed: bool = False
    notes: str = ""


class TaskPlan(BaseModel):
    """Full Mobius Planner output: subquestions, tasks, clarifications, retry, safety."""
    message_summary: str = ""
    subquestions: list[TaskPlanSubQuestion] = Field(default_factory=list)
    clarifications: list[ClarificationItem] = Field(default_factory=list)
    tasks: list[TaskItem] = Field(default_factory=list)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    safety: SafetyInfo = Field(default_factory=SafetyInfo)


# Fix forward ref
Plan.model_rebuild()
