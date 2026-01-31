"""Pydantic models for the planner: subquestions and plan."""
from pydantic import BaseModel, Field
from typing import Literal


class SubQuestion(BaseModel):
    """One subquestion from decomposition, with patient vs non-patient classification."""
    id: str = Field(..., description="Unique id for this subquestion (e.g. sq1, sq2)")
    text: str = Field(..., description="The subquestion text")
    kind: Literal["patient", "non_patient"] = Field(
        ...,
        description="patient = we do not have access (warning only); non_patient = RAG path",
    )


class Plan(BaseModel):
    """Output of the planner: list of subquestions and optional thinking log."""
    subquestions: list[SubQuestion] = Field(default_factory=list)
    thinking_log: list[str] = Field(
        default_factory=list,
        description="Chunks emitted during planning (for 'thinking' display)",
    )
