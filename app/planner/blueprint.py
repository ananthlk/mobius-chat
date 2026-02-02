"""Parser 2: Blueprint per subquestion (sensitivity, RAG strategy, agent) derived from Plan."""
from typing import Literal

from app.planner.schemas import Plan, SubQuestion
from app.trace_log import trace_entered

Sensitivity = Literal["low", "medium", "high"]
AgentType = Literal["RAG", "patient_stub"]


def _sensitivity_for(sq: SubQuestion) -> Sensitivity:
    """Derive sensitivity from kind and intent. High = personal/rigorous; low = general policy."""
    if sq.kind == "patient":
        return "high"
    intent = sq.question_intent or ""
    if intent == "factual":
        return "medium"
    return "low"


def build_blueprint(plan: Plan, rag_default_k: int = 10) -> list[dict]:
    """Build Parse 2 Blueprint: one entry per subquestion with sensitivity, RAG config, agent."""
    trace_entered("planner.blueprint.build_blueprint", subquestions=len(plan.subquestions))
    out: list[dict] = []
    for sq in plan.subquestions:
        agent: AgentType = "RAG" if sq.kind == "non_patient" else "patient_stub"
        sensitivity = _sensitivity_for(sq)
        rag_k = rag_default_k if sq.kind == "non_patient" else 0
        retrieval_config = "standard"
        out.append({
            "sq_id": sq.id,
            "agent": agent,
            "sensitivity": sensitivity,
            "rag_k": rag_k,
            "retrieval_config": retrieval_config,
            "kind": sq.kind,
            "intent": sq.question_intent or "—",
            "text": sq.text,
        })
    return out
