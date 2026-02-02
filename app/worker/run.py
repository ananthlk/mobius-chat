"""Worker: consume from request queue → planner (breakdown only) → answer per subquestion (patient / non-patient path) → combine → publish."""
import logging
import os
import threading
import time
from typing import Any, Callable

from pathlib import Path

_chat_root = Path(__file__).resolve().parent.parent.parent
# Load env: module .env first, then global mobius-config/.env (same as chat_config)
import sys
_config_dir = _chat_root.parent / "mobius-config"
if _config_dir.exists() and str(_config_dir) not in sys.path:
    sys.path.insert(0, str(_config_dir))
try:
    from env_helper import load_env
    load_env(_chat_root)
    _env_source = "env_helper.load_env"
except ImportError:
    from dotenv import load_dotenv
    load_dotenv(_chat_root / ".env", override=True)
    _env_source = "dotenv(_chat_root/.env)"
    # Clear placeholder credentials and resolve to credentials/*.json when env_helper not available
    _c = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or ""
    if "/path/to/" in _c or "your-service-account" in _c or "your-" in _c.lower():
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        for _d in (_chat_root / "credentials", _chat_root.parent / "mobius-config" / "credentials"):
            if _d.exists():
                for _p in _d.glob("*.json"):
                    if _p.is_file():
                        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_p.resolve())
                        break
                if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                    break

# Ensure Vertex project ID is always set (SDK and get_chat_config read env)
_vp = (os.environ.get("VERTEX_PROJECT_ID") or os.environ.get("CHAT_VERTEX_PROJECT_ID") or "mobiusos-new").strip()
if not _vp:
    _vp = "mobiusos-new"
os.environ.setdefault("VERTEX_PROJECT_ID", _vp)
os.environ.setdefault("CHAT_VERTEX_PROJECT_ID", os.environ.get("VERTEX_PROJECT_ID", "mobiusos-new"))

# Debug: log env and .env state at worker startup (root cause for RAG deployed index)
_log_env_path = _chat_root / ".env"
logger_startup = logging.getLogger(__name__)
_vert_id = os.environ.get("VERTEX_DEPLOYED_INDEX_ID", "")
_vert_proj = os.environ.get("VERTEX_PROJECT_ID", "")
_vert_ep = os.environ.get("VERTEX_INDEX_ENDPOINT_ID", "")
_rag_db = "set" if os.environ.get("CHAT_RAG_DATABASE_URL") else "unset"
print(
    f"[worker startup] _chat_root={_chat_root} .env_exists={_log_env_path.exists()} "
    f"VERTEX_PROJECT_ID={_vert_proj!r} VERTEX_INDEX_ENDPOINT_ID={'set' if _vert_ep else 'unset'} "
    f"VERTEX_DEPLOYED_INDEX_ID={_vert_id!r} CHAT_RAG_DATABASE_URL={_rag_db}",
    flush=True,
)
logger_startup.info(
    "[worker startup] _chat_root=%s VERTEX_PROJECT_ID=%r VERTEX_INDEX_ENDPOINT_ID=%s VERTEX_DEPLOYED_INDEX_ID=%r CHAT_RAG_DATABASE_URL=%s",
    _chat_root,
    _vert_proj,
    "set" if _vert_ep else "unset",
    _vert_id,
    _rag_db,
)

from app.chat_config import get_chat_config
from app.planner import parse
from app.planner.blueprint import build_blueprint
from app.planner.schemas import Plan
from app.queue import get_queue
from app.responder import format_response
from app.storage import store_plan, store_response
from app.storage.progress import append_message_chunk, append_thinking, clear_progress, start_progress
from app.services.cost_model import compute_cost
from app.services.non_patient_rag import answer_non_patient
from app.services.retrieval_calibration import get_retrieval_blend, intent_to_score
from app.services.usage import LLMUsageDict
from app.trace_log import trace_entered

logger = logging.getLogger(__name__)

DEBUG_PREFIX = "[debug]"
TRUNCATE = 200


def _debug_log_block(title: str, lines: list[str]) -> None:
    """Log a debug section with a clear header and indented lines."""
    logger.info("%s === %s ===", DEBUG_PREFIX, title)
    for line in lines:
        logger.info("%s   %s", DEBUG_PREFIX, line)
    logger.info("%s", DEBUG_PREFIX)


def _answer_for_subquestion(
    sq_id: str,
    kind: str,
    text: str,
    retrieval_params: dict[str, Any] | None = None,
    emitter=None,
) -> tuple[str, LLMUsageDict | None, list[dict]]:
    """Answer one subquestion: patient path = warning; non-patient path = RAG + LLM (with sources). Returns (answer_text, llm_usage, sources)."""
    def emit(msg: str) -> None:
        if emitter and msg.strip():
            emitter(msg.strip())

    if kind == "patient":
        emit("This part is about your own info—I can’t access that yet.")
        return ("I don’t have access to your personal records yet.", None, [])
    snippet = (text[:60] + "...") if len(text) > 60 else text
    emit(f"Answering this part: “{snippet}”")
    params = retrieval_params or get_retrieval_blend(0.5)
    answer_text, sources, usage = answer_non_patient(
        question=text,
        k=params.get("top_k"),
        confidence_min=params.get("confidence_min"),
        n_hierarchical=params.get("n_hierarchical"),
        n_factual=params.get("n_factual"),
        emitter=emitter,
    )
    return (answer_text, usage, sources or [])


def process_one(correlation_id: str, payload: dict) -> None:
    """Process one request: plan (breakdown only) → answer each subquestion via patient/non-patient path → combine → publish."""
    trace_entered("worker.run.process_one", correlation_id=correlation_id)
    message = payload.get("message", "").strip()
    thinking_chunks: list[str] = []
    start_progress(correlation_id)

    def on_thinking(chunk: str) -> None:
        thinking_chunks.append(chunk)
        append_thinking(correlation_id, chunk)
        logger.info("[thinking] %s", chunk[:80])

    plan = parse(message, thinking_emitter=on_thinking)
    store_plan(correlation_id, plan, thinking_log=thinking_chunks)

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

    # Answer each subquestion: patient = stub; non_patient = RAG + LLM (emit progress); collect usage and sources
    answers: list[str] = []
    usages: list[LLMUsageDict] = []
    all_sources: list[dict] = []
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
        ans, usage, sources = _answer_for_subquestion(
            sq.id, sq.kind, sq.text,
            retrieval_params=retrieval_params,
            emitter=on_thinking,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        answers.append(ans)
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

    def on_message_chunk(chunk: str) -> None:
        append_message_chunk(correlation_id, chunk)

    final_message, integrator_usage = format_response(
        plan, answers, user_message=message, emitter=on_thinking, message_chunk_callback=on_message_chunk
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
    }
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
