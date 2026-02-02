"""Pydantic models for the planner: subquestions and plan."""
from pydantic import BaseModel, Field
from typing import Any, Literal

QuestionIntent = Literal["factual", "canonical"]


class SubQuestion(BaseModel):
    """One subquestion from decomposition, with patient vs non-patient and question intent."""
    id: str = Field(..., description="Unique id for this subquestion (e.g. sq1, sq2)")
    text: str = Field(..., description="The subquestion text")
    kind: Literal["patient", "non_patient"] = Field(
        ...,
        description="patient = we do not have access (warning only); non_patient = RAG path",
    )
    question_intent: QuestionIntent | None = Field(
        default=None,
        description="factual = specific fact/lookup; canonical = policy/process description. Used to prioritize RAG retrieval.",
    )
    intent_score: float | None = Field(
        default=None,
        description="Numeric blend in [0, 1]: 0 = canonical (hierarchical retrieval), 1 = factual. Drives mix of hierarchical vs factual retrieval.",
    )


class Plan(BaseModel):
    """Output of the planner: list of subquestions and optional thinking log."""
    subquestions: list[SubQuestion] = Field(default_factory=list)
    thinking_log: list[str] = Field(
        default_factory=list,
        description="Chunks emitted during planning (for 'thinking' display)",
    )
    llm_usage: dict[str, Any] | None = Field(
        default=None,
        description="LLM usage for planning call (provider, model, input_tokens, output_tokens) for billing.",
    )
