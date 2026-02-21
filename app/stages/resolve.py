"""Stage: route subquestions to agents, collect answers."""
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_NO_SOURCES,
)
from app.services.non_patient_rag import answer_non_patient
from app.services.reasoning_agent import answer_reasoning
from app.services.tool_agent import answer_tool
from app.services.retrieval_calibration import get_retrieval_blend, intent_to_score
from app.services.usage import LLMUsageDict

if TYPE_CHECKING:
    from app.pipeline.context import PipelineContext


def _answer_for_subquestion(
    correlation_id: str,
    sq_id: str,
    agent: str,
    kind: str,
    text: str,
    retrieval_params: dict[str, Any] | None = None,
    emitter: Callable[[str], None] | None = None,
    rag_filter_overrides: dict[str, str] | None = None,
    include_document_ids: list[str] | None = None,
    on_rag_fail: list[str] | None = None,
) -> tuple[str, LLMUsageDict | None, list[dict], str]:
    """Answer one subquestion. Routes by agent: patient_stub, tool, reasoning, RAG."""
    def emit(msg: str) -> None:
        if emitter and msg and str(msg).strip():
            emitter(str(msg).strip())

    if agent == "patient_stub":
        emit("This part is about your own info—I can't access that yet.")
        return ("I don't have access to your personal records yet.", None, [], RETRIEVAL_SIGNAL_NO_SOURCES)

    if agent == "reasoning":
        snippet = (text[:60] + "...") if len(text) > 60 else text
        emit(f"Thinking through this: \"{snippet}\"")
        answer, usage = answer_reasoning(text, emitter=emitter)
        return (answer, usage, [], RETRIEVAL_SIGNAL_NO_SOURCES)

    if agent == "tool":
        snippet = (text[:60] + "...") if len(text) > 60 else text
        emit(f"Checking capabilities: \"{snippet}\"")
        answer, sources, usage, signal = answer_tool(
            text, emitter=emitter, invoke_google_for_search_request=True
        )
        return (answer, usage, sources or [], signal)

    # RAG path
    snippet = (text[:60] + "...") if len(text) > 60 else text
    emit(f"Answering this part: \"{snippet}\"")
    params = retrieval_params or get_retrieval_blend(0.5)
    on_fail = (on_rag_fail or []) if isinstance(on_rag_fail, list) else []
    answer_text, sources, usage, retrieval_signal = answer_non_patient(
        question=text,
        k=params.get("top_k"),
        confidence_min=params.get("confidence_min"),
        n_hierarchical=params.get("n_hierarchical"),
        n_factual=params.get("n_factual"),
        emitter=emitter,
        correlation_id=correlation_id,
        subquestion_id=sq_id,
        rag_filter_overrides=rag_filter_overrides,
        include_document_ids=include_document_ids,
        on_rag_fail=on_fail,
    )
    return (answer_text, usage, sources or [], retrieval_signal)


def run_resolve(
    ctx: "PipelineContext",
    emitter: Callable[[str], None] | None = None,
) -> None:
    """Answer each subquestion, populate ctx.answers, ctx.sources, ctx.usages, ctx.retrieval_signals."""
    plan = ctx.plan
    if not plan:
        return

    from app.state.jurisdiction import rag_filters_from_active

    rag_filter_overrides = rag_filters_from_active((ctx.merged_state or {}).get("active")) or {}
    include_document_ids = [s["document_id"] for s in (ctx.last_turn_sources or []) if s.get("document_id")]
    blueprint = ctx.blueprint
    plan_usage = getattr(plan, "llm_usage", None)
    usages: list[dict] = [plan_usage] if plan_usage else []
    answers: list[str] = []
    all_sources: list[dict] = []
    retrieval_signals: list[str] = []

    for i, sq in enumerate(plan.subquestions):
        bp = blueprint[i] if i < len(blueprint) else {}
        retrieval_params = None
        agent = bp.get("agent") or ("RAG" if sq.kind == "non_patient" else "patient_stub")
        if agent == "RAG":
            score = getattr(sq, "intent_score", None)
            if score is None:
                score = intent_to_score(getattr(sq, "question_intent", None))
            retrieval_params = get_retrieval_blend(score)

        question_text = bp.get("reframed_text") or bp.get("text") or sq.text
        on_rag_fail = bp.get("on_rag_fail") if isinstance(bp.get("on_rag_fail"), list) else None
        ans, usage, sources, retrieval_signal = _answer_for_subquestion(
            ctx.correlation_id,
            sq.id,
            agent,
            sq.kind,
            question_text,
            retrieval_params=retrieval_params,
            emitter=emitter,
            rag_filter_overrides=rag_filter_overrides or None,
            include_document_ids=include_document_ids or None,
            on_rag_fail=on_rag_fail,
        )
        answers.append(ans)
        retrieval_signals.append(retrieval_signal)
        if usage:
            usages.append(usage)
        for s in sources or []:
            all_sources.append({**s, "index": len(all_sources) + 1})

    ctx.answers = answers
    ctx.sources = all_sources
    ctx.usages = usages
    ctx.retrieval_signals = retrieval_signals
