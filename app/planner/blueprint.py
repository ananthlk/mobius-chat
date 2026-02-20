"""Parser 2: Blueprint per subquestion (sensitivity, RAG strategy, agent) derived from Plan."""
from typing import Literal

from app.planner.schemas import Plan, SubQuestion
from app.state.query_refinement import reframe_for_retrieval
from app.trace_log import trace_entered

Sensitivity = Literal["low", "medium", "high"]
AgentType = Literal["RAG", "patient_stub", "tool"]


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
        if sq.kind == "tool":
            agent: AgentType = "tool"
        elif sq.kind == "non_patient":
            agent = "RAG"
        else:
            agent = "patient_stub"
        sensitivity = _sensitivity_for(sq)
        rag_k = rag_default_k if sq.kind == "non_patient" else 0
        retrieval_config = "standard"
        reframed = reframe_for_retrieval(
            sq.text,
            intent=sq.question_intent,
            question_intent=sq.question_intent,
        )
        out.append({
            "sq_id": sq.id,
            "agent": agent,
            "sensitivity": sensitivity,
            "rag_k": rag_k,
            "retrieval_config": retrieval_config,
            "kind": sq.kind,
            "intent": sq.question_intent or "—",
            "text": sq.text,
            "reframed_text": reframed if reframed != sq.text else sq.text,
        })
    return out
