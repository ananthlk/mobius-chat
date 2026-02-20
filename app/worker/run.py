"""Worker: consume from request queue → planner (breakdown only) → answer per subquestion (patient / non-patient path) → combine → publish."""
import json
import logging
import threading
import time
from typing import Any, Callable

from dotenv import load_dotenv
load_dotenv()  # load .env from project root (same credentials as Mobius RAG when using Vertex)

from app.chat_config import get_chat_config, get_config_sha
from app.planner import parse
from app.planner.blueprint import build_blueprint
from app.planner.schemas import Plan
from app.queue import get_queue
from app.responder import format_response
from app.state.clarification import need_jurisdiction_clarification
from app.state.state_extractor import extract_state_patch
from app.storage import insert_turn, store_plan, store_response
from app.storage.progress import append_message_chunk, append_thinking, clear_progress, start_progress
from app.state.context_pack import build_context_pack
from app.state.context_router import route_context
from app.state.jurisdiction import get_jurisdiction_from_active, jurisdiction_to_summary, rag_filters_from_active
from app.storage.threads import (
    DEFAULT_STATE,
    append_turn_messages,
    get_last_turn_messages,
    get_state,
    register_open_slots,
    save_state,
)
from app.services.cost_model import compute_cost
from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_CORPUS_ONLY,
    RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE,
    RETRIEVAL_SIGNAL_GOOGLE_ONLY,
    RETRIEVAL_SIGNAL_NO_SOURCES,
)
from app.services.non_patient_rag import answer_non_patient
from app.services.retrieval_calibration import get_retrieval_blend, intent_to_score
from app.services.usage import LLMUsageDict

logger = logging.getLogger(__name__)

DEBUG_PREFIX = "[debug]"
TRUNCATE = 200

# Badge keys for source_confidence_strip (Option D: retrieval default, LLM may override)
BADGE_APPROVED_AUTHORITATIVE = "approved_authoritative"
BADGE_APPROVED_INFORMATIONAL = "approved_informational"
BADGE_PROCEED_WITH_CAUTION = "proceed_with_caution"
BADGE_AUGMENTED_WITH_GOOGLE = "augmented_with_google"
BADGE_INFORMATIONAL_ONLY = "informational_only"
BADGE_NO_SOURCES = "no_sources"


def _default_source_confidence(retrieval_signals: list[str], all_sources: list[dict]) -> str:
    """Compute default badge from retrieval signals (worst wins)."""
    if not retrieval_signals:
        return BADGE_NO_SOURCES
    # Worst to best: no_sources, google_only, corpus_plus_google, corpus_only
    if RETRIEVAL_SIGNAL_NO_SOURCES in retrieval_signals:
        return BADGE_NO_SOURCES
    if RETRIEVAL_SIGNAL_GOOGLE_ONLY in retrieval_signals:
        return BADGE_INFORMATIONAL_ONLY
    if RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE in retrieval_signals:
        return BADGE_AUGMENTED_WITH_GOOGLE
    # corpus_only: check confidence labels in sources
    labels = [s.get("confidence_label") for s in all_sources if s.get("confidence_label")]
    if any(l == "process_with_caution" for l in labels):
        return BADGE_PROCEED_WITH_CAUTION
    if all(l == "process_confident" for l in labels) and labels:
        return BADGE_APPROVED_AUTHORITATIVE
    if labels:
        return BADGE_APPROVED_INFORMATIONAL
    return BADGE_APPROVED_INFORMATIONAL


def _debug_log_block(title: str, lines: list[str]) -> None:
    """Log a debug section with a clear header and indented lines."""
    logger.info("%s === %s ===", DEBUG_PREFIX, title)
    for line in lines:
        logger.info("%s   %s", DEBUG_PREFIX, line)
    logger.info("%s", DEBUG_PREFIX)


def _answer_for_subquestion(
    correlation_id: str,
    sq_id: str,
    kind: str,
    text: str,
    retrieval_params: dict[str, Any] | None = None,
    emitter=None,
    rag_filter_overrides: dict[str, str] | None = None,
) -> tuple[str, LLMUsageDict | None, list[dict], str]:
    """Answer one subquestion: patient path = warning; non-patient path = RAG + LLM (with sources). Returns (answer_text, llm_usage, sources)."""
    def emit(msg: str) -> None:
        if emitter and msg.strip():
            emitter(msg.strip())

    if kind == "patient":
        emit("This part is about your own info—I can’t access that yet.")
        return ("I don’t have access to your personal records yet.", None, [], RETRIEVAL_SIGNAL_NO_SOURCES)
    snippet = (text[:60] + "...") if len(text) > 60 else text
    emit(f"Answering this part: “{snippet}”")
    params = retrieval_params or get_retrieval_blend(0.5)
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
    )
    return (answer_text, usage, sources or [], retrieval_signal)


def process_one(correlation_id: str, payload: dict) -> None:
    """Process one request: plan (breakdown only) → answer each subquestion via patient/non-patient path → combine → publish."""
    t0_start = time.perf_counter()
    message = payload.get("message", "").strip()
    thread_id = (payload.get("thread_id") or "").strip() or None
    thinking_chunks: list[str] = []
    start_progress(correlation_id)

    def on_thinking(chunk: str) -> None:
        thinking_chunks.append(chunk)
        append_thinking(correlation_id, chunk)
        logger.info("[thinking] %s", chunk[:80])

    # Load state and extract jurisdiction when thread_id provided
    active: dict | None = None
    merged_state: dict | None = None
    context_pack = ""
    if thread_id:
        state = get_state(thread_id)
        if state is None:
            state = json.loads(json.dumps(DEFAULT_STATE))
        patch, reset_reason = extract_state_patch(message, state, parse1_output=None, answer_card=None)
        if patch:
            save_state(thread_id, patch)
        merged_state = {**state}
        for k, v in (patch or {}).items():
            if isinstance(merged_state.get(k), dict) and isinstance(v, dict):
                merged_state[k] = {**merged_state.get(k, {}), **v}
            else:
                merged_state[k] = v
        active = merged_state.get("active")
        last_turns = get_last_turn_messages(thread_id)
        route = route_context(message, merged_state, last_turns, reset_reason=reset_reason)
        context_pack = build_context_pack(route, merged_state, last_turns, merged_state.get("open_slots") or [])

    rag_filter_overrides = rag_filters_from_active((merged_state or {}).get("active")) if merged_state else {}

    plan = parse(message, thinking_emitter=on_thinking, context=context_pack)
    store_plan(correlation_id, plan, thinking_log=thinking_chunks)

    # Jurisdiction clarification: ask when we have non_patient questions but no payor/state/program.
    # Run even without thread_id (use empty active) so users see the ask; only register_open_slots when thread_id exists.
    active_for_clarification = active if active is not None else (merged_state.get("active") if merged_state else {}) or {}
    needs_clarification, missing_slots, clarification_message = need_jurisdiction_clarification(
        plan.subquestions, active_for_clarification
    )
    if needs_clarification and clarification_message:
        if thread_id and missing_slots:
            register_open_slots(thread_id, missing_slots)
        response_payload = {
            "status": "clarification",
            "message": clarification_message,
            "plan": plan.model_dump(),
            "thinking_log": thinking_chunks,
            "open_slots": missing_slots,
            "response_source": "clarification",
            "model_used": None,
            "llm_error": None,
            "tokens_used": {"input_tokens": 0, "output_tokens": 0},
            "usage_breakdown": [],
            "cost_usd": 0.0,
            "sources": [],
            "source_confidence_strip": None,
            "cited_source_indices": [],
            "thread_id": thread_id,
        }
        duration_ms = int((time.perf_counter() - t0_start) * 1000)
        try:
            config_sha = get_config_sha() or None
        except Exception:
            config_sha = None
        try:
            insert_turn(
                correlation_id=correlation_id,
                question=message,
                thinking_log=thinking_chunks,
                final_message=clarification_message,
                sources=[],
                duration_ms=duration_ms,
                model_used=None,
                llm_provider=None,
                session_id=None,
                thread_id=thread_id,
                plan_snapshot=plan.model_dump(),
                source_confidence_strip=None,
                config_sha=config_sha,
            )
        except Exception as e:
            logger.warning("Failed to persist clarification turn: %s", e)
        if thread_id:
            try:
                append_turn_messages(thread_id, correlation_id, message, clarification_message)
            except Exception as e:
                logger.warning("Failed to append clarification turn messages: %s", e)
        clear_progress(correlation_id)
        store_response(correlation_id, response_payload)
        get_queue().publish_response(correlation_id, response_payload)
        logger.info("Jurisdiction clarification returned for %s", correlation_id)
        return

    # --- DEBUG: Parse 1 (Plan) ---
    parse1_lines = [
        f"user_message: {message[:80]}{'...' if len(message) > 80 else ''}",
        f"subquestions: {len(plan.subquestions)}",
    ]
    for sq in plan.subquestions:
        intent = getattr(sq, "question_intent", None) or "—"
        score = getattr(sq, "intent_score", None)
        parse1_lines.append(f"  {sq.id}: kind={sq.kind} intent={intent} intent_score={score} text={sq.text[:60]}{'...' if len(sq.text) > 60 else ''}")
    if getattr(plan, "llm_usage", None):
        u = plan.llm_usage
        parse1_lines.append(f"  llm_usage: provider={u.get('provider')} model={u.get('model')} in={u.get('input_tokens')} out={u.get('output_tokens')}")
    _debug_log_block("Parse 1 (Plan)", parse1_lines)

    # --- DEBUG: Parse 2 (Blueprint) ---
    rag_k_default = get_chat_config().rag.top_k
    blueprint = build_blueprint(plan, rag_default_k=rag_k_default)
    parse2_lines = ["Pipeline: Planner → [per subquestion] → Integrator"]
    for entry in blueprint:
        parse2_lines.append(
            f"  {entry['sq_id']}: agent={entry['agent']} sensitivity={entry['sensitivity']} rag_k={entry['rag_k']} "
            f"retrieval_config={entry['retrieval_config']} kind={entry['kind']} intent={entry['intent']}"
        )
        parse2_lines.append(f"    text: {entry['text'][:80]}{'...' if len(entry['text']) > 80 else ''}")
    _debug_log_block("Parse 2 (Blueprint)", parse2_lines)

    # --- DEBUG: Agent I/O cards ---
    # Planner I/O
    planner_out_lines = [f"subquestions: {len(plan.subquestions)}"]
    for sq in plan.subquestions:
        intent = getattr(sq, "question_intent", None) or "—"
        planner_out_lines.append(f"  {sq.id}: kind={sq.kind} intent={intent} text={sq.text[:60]}{'...' if len(sq.text) > 60 else ''}")
    if getattr(plan, "llm_usage", None):
        u = plan.llm_usage
        planner_out_lines.append(f"  llm_usage: provider={u.get('provider')} model={u.get('model')} in={u.get('input_tokens')} out={u.get('output_tokens')}")
    _debug_log_block("Agent I/O: Planner", ["INPUT: user_message", f"  {message[:TRUNCATE]}{'...' if len(message) > TRUNCATE else ''}", "OUTPUT: plan"] + planner_out_lines)

    # Answer each subquestion: patient = stub; non_patient = RAG + LLM (emit progress); collect usage, sources, retrieval_signals
    answers: list[str] = []
    usages: list[LLMUsageDict] = []
    all_sources: list[dict] = []
    retrieval_signals: list[str] = []
    if getattr(plan, "llm_usage", None):
        usages.append(plan.llm_usage)
    for i, sq in enumerate(plan.subquestions):
        agent_name = "RAG" if sq.kind == "non_patient" else "patient_stub"
        bp = blueprint[i] if i < len(blueprint) else {}
        retrieval_params = None
        if sq.kind == "non_patient":
            score = getattr(sq, "intent_score", None)
            if score is None:
                score = intent_to_score(getattr(sq, "question_intent", None))
            retrieval_params = get_retrieval_blend(score)
        t0 = time.perf_counter()
        ans, usage, sources, retrieval_signal = _answer_for_subquestion(
            correlation_id, sq.id, sq.kind, sq.text,
            retrieval_params=retrieval_params,
            emitter=on_thinking,
            rag_filter_overrides=rag_filter_overrides or None,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        answers.append(ans)
        retrieval_signals.append(retrieval_signal)
        if usage:
            usages.append(usage)
        for s in sources or []:
            all_sources.append({**s, "index": len(all_sources) + 1})
        # RAG / patient_stub I/O card
        in_lines = [f"INPUT: question (sq_id={sq.id})", f"  {sq.text[:TRUNCATE]}{'...' if len(sq.text) > TRUNCATE else ''}"]
        if sq.kind == "non_patient" and retrieval_params:
            nh, nf = retrieval_params.get("n_hierarchical", 0), retrieval_params.get("n_factual", 0)
            score = getattr(sq, "intent_score", None)
            intent = getattr(sq, "question_intent", None) or "—"
            in_lines.append(f"  retrieval: n_hierarchical={nh} n_factual={nf} confidence_min={retrieval_params.get('confidence_min')} (intent_score={score} intent={intent}; 0=canonical→hierarchical, 1=factual)")
        elif sq.kind == "non_patient":
            in_lines.append(f"  rag_k={bp.get('rag_k', rag_k_default)} sensitivity={bp.get('sensitivity', '—')}")
        out_lines = ["OUTPUT: answer", f"  {ans[:TRUNCATE]}{'...' if len(ans) > TRUNCATE else ''}"]
        if sources:
            out_lines.append(f"  retrieved_docs: {len(sources)}")
            stypes = {s.get("source_type") or "—" for s in sources}
            if len(stypes) == 1 and len(sources) > 1:
                out_lines.append(f"  (all source_type={next(iter(stypes))}; hierarchy had no diversity—index may only have this type)")
            for s in sources[:10]:
                doc_name = s.get("document_name") or "document"
                page = s.get("page_number")
                stype = s.get("source_type") or "—"
                match = s.get("match_score")
                conf = s.get("confidence")
                match_str = f" match={match:.2f}" if match is not None else ""
                conf_str = f" confidence={conf:.2f}" if conf is not None else ""
                out_lines.append(f"    [{s.get('index', '?')}] {doc_name}" + (f" (page {page})" if page is not None else "") + f" type={stype}{match_str}{conf_str}")
            if len(sources) > 10:
                out_lines.append(f"    ... and {len(sources) - 10} more")
        if usage:
            out_lines.append(f"  usage: in={usage.get('input_tokens')} out={usage.get('output_tokens')} cost_usd={round(compute_cost(usage), 6)}")
        out_lines.append(f"  duration_ms: {elapsed_ms:.0f}")
        _debug_log_block(f"Agent I/O: {agent_name} {sq.id}", in_lines + out_lines)

    t0_integ = time.perf_counter()
    logger.info("%s [Agent: Integrator] input: plan + %d answers → output: combined message", DEBUG_PREFIX, len(answers))

    default_source_confidence = _default_source_confidence(retrieval_signals, all_sources)
    retrieval_metadata = {
        "default_source_confidence": default_source_confidence,
        "instruction": "We expect you to use the highest-rated document(s). If you override, set source_confidence_override and explain in confidence_note.",
    }
    sources_summary = [
        {"index": s.get("index", i + 1), "document_name": s.get("document_name") or "document", "confidence_label": s.get("confidence_label")}
        for i, s in enumerate(all_sources)
    ]

    def on_message_chunk(chunk: str) -> None:
        append_message_chunk(correlation_id, chunk)

    jurisdiction_summary = None
    if active:
        j = get_jurisdiction_from_active(active)
        jurisdiction_summary = jurisdiction_to_summary(j) or None
    final_message, integrator_usage = format_response(
        plan, answers, user_message=message, emitter=on_thinking, message_chunk_callback=on_message_chunk,
        retrieval_metadata=retrieval_metadata, sources_summary=sources_summary,
        jurisdiction_summary=jurisdiction_summary,
    )
    elapsed_integ_ms = (time.perf_counter() - t0_integ) * 1000
    if integrator_usage:
        usages.append(integrator_usage)
    # Integrator I/O card
    integ_in = ["INPUT: plan + N answers", f"  N={len(answers)}"] + [f"  answer_{i+1}: {a[:80]}{'...' if len(a) > 80 else ''}" for i, a in enumerate(answers[:5])]
    if len(answers) > 5:
        integ_in.append(f"  ... and {len(answers) - 5} more")
    integ_out = ["OUTPUT: combined message", f"  {final_message[:TRUNCATE]}{'...' if len(final_message) > TRUNCATE else ''}"]
    if integrator_usage:
        integ_out.append(f"  usage: in={integrator_usage.get('input_tokens')} out={integrator_usage.get('output_tokens')} cost_usd={round(compute_cost(integrator_usage), 6)}")
    integ_out.append(f"  duration_ms: {elapsed_integ_ms:.0f}")
    _debug_log_block("Agent I/O: Integrator", integ_in + integ_out)
    logger.info("%s [Agent: Integrator] done.", DEBUG_PREFIX)

    # Aggregate tokens and cost for billing / cost-plus pricing
    total_input = sum(int(u.get("input_tokens") or 0) for u in usages)
    total_output = sum(int(u.get("output_tokens") or 0) for u in usages)
    total_cost = sum(compute_cost(u) for u in usages)
    usage_breakdown: list[dict[str, Any]] = []
    has_plan_usage = bool(getattr(plan, "llm_usage", None))
    for i, u in enumerate(usages):
        if i == 0 and has_plan_usage:
            stage = "plan"
        elif integrator_usage is not None and i == len(usages) - 1:
            stage = "integrator"
        else:
            stage = "rag"
        usage_breakdown.append({
            "stage": stage,
            "model": u.get("model") or "",
            "provider": u.get("provider") or "",
            "input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
            "cost_usd": round(compute_cost(u), 6),
        })
    model_used = (usages[0].get("model") or None) if usages else None

    # Source cards for chat (from RAG; not dependent on integrator message)
    response_sources = [
        {
            "index": s.get("index", i + 1),
            "document_name": s.get("document_name") or "document",
            "page_number": s.get("page_number"),
            "source_type": s.get("source_type"),
            "match_score": s.get("match_score"),
            "confidence": s.get("confidence"),
            "text": (s.get("text") or "")[:200],
        }
        for i, s in enumerate(all_sources)
    ]

    # Option D: source_confidence_strip = override ?? default; cited_source_indices from AnswerCard
    source_confidence_strip = default_source_confidence
    cited_source_indices: list[int] = []
    try:
        parsed = json.loads(final_message)
        if isinstance(parsed, dict):
            override = parsed.get("source_confidence_override")
            if override and str(override).strip() in (
                BADGE_APPROVED_AUTHORITATIVE, BADGE_APPROVED_INFORMATIONAL, BADGE_PROCEED_WITH_CAUTION,
                BADGE_AUGMENTED_WITH_GOOGLE, BADGE_INFORMATIONAL_ONLY, BADGE_NO_SOURCES,
            ):
                source_confidence_strip = str(override).strip()
            indices = parsed.get("cited_source_indices")
            if isinstance(indices, list):
                cited_source_indices = [int(x) for x in indices if isinstance(x, (int, float)) and 1 <= int(x) <= len(all_sources)]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    duration_ms = int((time.perf_counter() - t0_start) * 1000)
    try:
        config_sha = get_config_sha() or None
    except Exception:
        config_sha = None
    response_payload = {
        "status": "completed",
        "message": final_message,
        "plan": plan.model_dump(),
        "thinking_log": thinking_chunks,
        "response_source": "plan",
        "model_used": model_used,
        "llm_error": None,
        "tokens_used": {"input_tokens": total_input, "output_tokens": total_output},
        "usage_breakdown": usage_breakdown,
        "cost_usd": round(total_cost, 6),
        "sources": response_sources,
        "source_confidence_strip": source_confidence_strip,
        "cited_source_indices": cited_source_indices,
        "thread_id": thread_id,
    }
    try:
        insert_turn(
            correlation_id=correlation_id,
            question=message,
            thinking_log=thinking_chunks,
            final_message=final_message,
            sources=response_sources,
            duration_ms=duration_ms,
            model_used=model_used,
            llm_provider=(usages[0].get("provider") if usages else None),
            session_id=None,
            thread_id=thread_id,
            plan_snapshot=plan.model_dump(),
            source_confidence_strip=source_confidence_strip,
            config_sha=config_sha,
        )
    except Exception as e:
        logger.warning("Failed to persist turn: %s", e)
    if thread_id:
        try:
            append_turn_messages(thread_id, correlation_id, message, final_message)
        except Exception as e:
            logger.warning("Failed to append turn messages: %s", e)
    clear_progress(correlation_id)
    store_response(correlation_id, response_payload)
    get_queue().publish_response(correlation_id, response_payload)
    logger.info("Response published for %s", correlation_id)


def run_worker() -> None:
    """Blocking: consume requests and process each."""
    q = get_queue()
    q.consume_requests(process_one)


def start_worker_background() -> threading.Thread:
    """Start the worker in a background thread. Returns the thread. Use for in-memory queue (single process)."""
    t = threading.Thread(target=run_worker, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    """Run worker standalone: python -m app.worker. Use with Redis (or other) queue so API and worker are separate."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(levelname)s %(message)s")
    run_worker()
