"""Worker: consume from request queue → planner (breakdown only) → answer per subquestion (patient / non-patient path) → combine → publish."""
import copy
import json
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
    _env_file = _chat_root / ".env"
    _preserve = {k: os.environ.get(k) for k in ("QUEUE_TYPE", "REDIS_URL") if os.environ.get(k)}
    load_dotenv(_env_file, override=True)
    for k, v in _preserve.items():
        if v is not None:
            os.environ[k] = v
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

# Clear placeholder GCP credentials and resolve to credentials/*.json if missing (safety net when env_helper loaded global .env with placeholder)
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

from app.chat_config import get_chat_config, get_config_sha
from app.planner import parse
from app.planner.blueprint import build_blueprint
from app.planner.schemas import Plan
from app.queue import get_queue
from app.responder import format_response
from app.storage import insert_turn, store_plan, store_response
from app.storage.progress import append_message_chunk, append_thinking, clear_progress, start_progress
from app.storage.threads import (
    DEFAULT_STATE,
    append_assistant_message,
    append_user_message,
    ensure_thread,
    get_last_turn_messages,
    get_state,
    register_open_slots,
    save_state,
)
from app.state.context_pack import build_context_pack
from app.state.context_router import route_context
from app.state.state_extractor import answer_card_to_open_slots, extract_state_patch
from app.payer_normalization import normalize_payer_for_rag
from app.services.cost_model import compute_cost
from app.services.non_patient_rag import answer_non_patient
from app.services.retrieval_calibration import get_retrieval_blend, intent_to_score
from app.services.usage import LLMUsageDict
from app.trace_log import trace_entered, is_trace_enabled

logger = logging.getLogger(__name__)

# Show trace status at startup so we can see why [trace] lines may be missing
_trace_val = os.environ.get("CHAT_DEBUG_TRACE") or os.environ.get("DEBUG_TRACE") or ""
print(f"[worker startup] CHAT_DEBUG_TRACE/DEBUG_TRACE={_trace_val!r} trace_enabled={is_trace_enabled()}", flush=True)
# Show queue_type so we can confirm progress will publish to Redis when redis
try:
    from app.config import get_config
    _cfg = get_config()
    print(f"[worker startup] queue_type={_cfg.queue_type!r} (progress publishes to Redis when redis)", flush=True)
except Exception as e:
    print(f"[worker startup] get_config: {e}", flush=True)

DEBUG_PREFIX = "[debug]"
TRUNCATE = 200


def _debug_log_block(title: str, lines: list[str]) -> None:
    """Log a debug section with a clear header and indented lines."""
    logger.info("%s === %s ===", DEBUG_PREFIX, title)
    for line in lines:
        logger.info("%s   %s", DEBUG_PREFIX, line)
    logger.info("%s", DEBUG_PREFIX)


# Confidence strip: allowed values for UI (frontend maps to locked copy)
# Authoritative: document_authority_level from published_rag_metadata (e.g. Sunshine = contract_source_of_truth); fallback to source_type
_CONFIDENCE_STRIP_AUTHORITY_LEVEL_VALUES = ("contract_source_of_truth", "authoritative", "authority", "approved")
_CONFIDENCE_STRIP_SOURCE_TYPE_AUTHORITATIVE = ("policy", "section")
_CONFIDENCE_PENDING_THRESHOLD = 0.5


def _derive_source_confidence_strip(sources: list[dict]) -> str:
    """Derive single confidence-strip value from aggregated RAG sources. Returns one of:
    approved_authoritative, approved_informational, pending, partial_pending, unverified.
    Uses document_authority_level (from published_rag_metadata) when present; else source_type (policy/section)."""
    if not sources:
        return "unverified"
    n_authoritative = 0
    n_pending = 0
    n_approved = 0
    for s in sources:
        auth_level = (s.get("document_authority_level") or "").strip().lower()
        st = (s.get("source_type") or "").strip().lower()
        if auth_level and auth_level in _CONFIDENCE_STRIP_AUTHORITY_LEVEL_VALUES:
            n_authoritative += 1
        elif st in _CONFIDENCE_STRIP_SOURCE_TYPE_AUTHORITATIVE:
            n_authoritative += 1
        conf = s.get("confidence")
        if conf is None or (isinstance(conf, (int, float)) and float(conf) < _CONFIDENCE_PENDING_THRESHOLD):
            n_pending += 1
        else:
            n_approved += 1
    if n_authoritative >= 1:
        return "approved_authoritative"
    if n_pending == len(sources):
        return "pending"
    if n_pending >= 1 and n_approved >= 1:
        return "partial_pending"
    return "approved_informational"


def _rag_filters_from_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Build per-request RAG filter dict from thread state. filter_payer: str (single) or list[str] (multi-payer compare).
    Payer is normalized via config/payer_normalization.yaml so RAG document_payer filter matches index tokens."""
    out: dict[str, Any] = {}
    if not state:
        return out
    active = state.get("active") or {}
    payers_list = active.get("payers") or []
    if payers_list and isinstance(payers_list, list):
        normalized = [normalize_payer_for_rag(p) for p in payers_list if p and normalize_payer_for_rag(p)]
        if normalized:
            out["filter_payer"] = normalized
    else:
        raw_payer = (active.get("payer") or "").strip()
        if raw_payer:
            payer = normalize_payer_for_rag(raw_payer)
            if payer:
                out["filter_payer"] = payer
    jurisdiction = (active.get("jurisdiction") or "").strip()
    if jurisdiction:
        out["filter_state"] = jurisdiction
    return out


def _answer_for_subquestion(
    sq_id: str,
    kind: str,
    text: str,
    retrieval_params: dict[str, Any] | None = None,
    emitter=None,
    rag_filters: dict[str, str] | None = None,
) -> tuple[str, LLMUsageDict | None, list[dict]]:
    """Answer one subquestion: patient path = warning; non-patient path = RAG + LLM (with sources). Returns (answer_text, llm_usage, sources).
    rag_filters (from thread state) scope RAG retrieval to payer/state when set."""
    def emit(msg: str) -> None:
        if emitter and msg.strip():
            emitter(msg.strip())

    if kind == "patient":
        emit("This part is about your own info—I can’t access that yet.")
        return ("I don’t have access to your personal records yet.", None, [])
    snippet = (text[:60] + "...") if len(text) > 60 else text
    emit(f"Answering this part: “{snippet}”")
    params = retrieval_params or get_retrieval_blend(0.5)
    filters = rag_filters or {}
    answer_text, sources, usage = answer_non_patient(
        question=text,
        k=params.get("top_k"),
        confidence_min=params.get("confidence_min"),
        n_hierarchical=params.get("n_hierarchical"),
        n_factual=params.get("n_factual"),
        emitter=emitter,
        filter_payer=filters.get("filter_payer"),
        filter_state=filters.get("filter_state"),
    )
    return (answer_text, usage, sources or [])


def _merge_state(state: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply patch shallowly to state (nested dicts merged). Returns new dict."""
    out = copy.deepcopy(state)
    for k, v in patch.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = {**out.get(k, {}), **v}
        else:
            out[k] = v
    return out


def process_one(correlation_id: str, payload: dict) -> None:
    """Process one request: plan (breakdown only) → answer each subquestion via patient/non-patient path → combine → publish."""
    trace_entered("worker.run.process_one", correlation_id=correlation_id)
    t0_total = time.perf_counter()
    config_sha = get_config_sha() or None
    message = payload.get("message", "").strip()
    thinking_chunks: list[str] = []
    start_progress(correlation_id)

    # Debug: acknowledge job immediately and publish same to Redis/DB so we can see if GET receives it
    ack_msg = "Worker picked up request."
    logger.info("Acknowledged job correlation_id=%s publishing to Redis/DB: %r", correlation_id[:8], ack_msg)
    append_thinking(correlation_id, ack_msg)

    # Resolve thread and persist user message for short-term memory
    thread_id = payload.get("thread_id") or ensure_thread(None)
    thread_id = ensure_thread(thread_id)  # ensure row exists so append_user_message FK is satisfied (API may have failed to create it)
    append_user_message(thread_id, correlation_id, message)

    # Load state and last turns; apply TTL decay (state should decay quickly)
    state = get_state(thread_id)
    if state is None:
        state = copy.deepcopy(DEFAULT_STATE)
    open_slots = (state.get("open_slots") or []) if state else []
    if open_slots:
        save_state(thread_id, {"turn_count_since_active_set": 0})
        state = get_state(thread_id) or state
        if state:
            state.setdefault("turn_count_since_active_set", 0)
    else:
        count = (state.get("turn_count_since_active_set") or 0) + 1
        state["turn_count_since_active_set"] = count
        if count > 2:
            save_state(thread_id, {
                "active": {**(state.get("active") or {}), "payer": None, "domain": None, "jurisdiction": None},
                "turn_count_since_active_set": 0,
            })
            state = get_state(thread_id) or state
        else:
            save_state(thread_id, {"turn_count_since_active_set": count})
            state = get_state(thread_id) or state
    last_turns = get_last_turn_messages(thread_id, limit_turns=2)
    patch, reset_reason = extract_state_patch(message, state, None, None)
    new_state = _merge_state(state, patch)
    # Log state extraction so we can see why payer might not switch (e.g. "United Healthcare" in message)
    existing_payer = (state.get("active") or {}).get("payer")
    patch_payer = (patch.get("active") or {}).get("payer")
    new_payer = (new_state.get("active") or {}).get("payer")
    logger.info(
        "state_extract: message=%r existing_payer=%s patch_payer=%s new_payer=%s reset_reason=%s",
        (message or "")[:120],
        existing_payer,
        patch_payer,
        new_payer,
        reset_reason,
    )
    save_state(thread_id, patch)
    route = route_context(message, new_state, last_turns, reset_reason)
    pack = build_context_pack(route, new_state, last_turns, new_state.get("open_slots", []))
    message_for_parser = (pack + "\n\n" + message) if pack else message

    def on_thinking(chunk: str) -> None:
        thinking_chunks.append(chunk)
        append_thinking(correlation_id, chunk)
        logger.info("[thinking] %s", chunk[:80])

    plan = parse(message_for_parser, thinking_emitter=on_thinking)
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

    plan_snapshot: dict[str, Any] = {
        "user_message_snippet": message[:TRUNCATE] + ("..." if len(message) > TRUNCATE else ""),
        "subquestions_count": len(plan.subquestions),
        "subquestions": [
            {
                "id": sq.id,
                "kind": sq.kind,
                "intent": getattr(sq, "question_intent", None) or "—",
                "intent_score": getattr(sq, "intent_score", None),
                "text_snippet": (sq.text or "")[:TRUNCATE] + ("..." if len(sq.text or "") > TRUNCATE else ""),
            }
            for sq in plan.subquestions
        ],
        "llm_usage": getattr(plan, "llm_usage", None),
    }

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

    blueprint_snapshot: dict[str, Any] = {
        "pipeline": "Planner → [per subquestion] → Integrator",
        "entries": [
            {
                "sq_id": e["sq_id"],
                "agent": e["agent"],
                "sensitivity": e["sensitivity"],
                "rag_k": e["rag_k"],
                "retrieval_config": str(e.get("retrieval_config", "")),
                "kind": e["kind"],
                "intent": e["intent"],
                "text_snippet": ((e.get("text") or "")[:TRUNCATE] + ("..." if len(e.get("text") or "") > TRUNCATE else "")),
            }
            for e in blueprint
        ],
    }

    # --- DEBUG: Agent I/O cards ---
    agent_cards: list[dict[str, Any]] = []
    # Planner I/O
    planner_out_lines = [f"subquestions: {len(plan.subquestions)}"]
    for sq in plan.subquestions:
        intent = getattr(sq, "question_intent", None) or "—"
        planner_out_lines.append(f"  {sq.id}: kind={sq.kind} intent={intent} text={sq.text[:60]}{'...' if len(sq.text) > 60 else ''}")
    if getattr(plan, "llm_usage", None):
        u = plan.llm_usage
        planner_out_lines.append(f"  llm_usage: provider={u.get('provider')} model={u.get('model')} in={u.get('input_tokens')} out={u.get('output_tokens')}")
    planner_in = ["INPUT: user_message", f"  {message[:TRUNCATE]}{'...' if len(message) > TRUNCATE else ''}"]
    planner_out = ["OUTPUT: plan"] + planner_out_lines
    _debug_log_block("Agent I/O: Planner", planner_in + planner_out)
    plan_usage = getattr(plan, "llm_usage", None)
    agent_cards.append({
        "stage": "Planner",
        "input_lines": planner_in,
        "output_lines": planner_out,
        "usage": {"input_tokens": plan_usage.get("input_tokens"), "output_tokens": plan_usage.get("output_tokens"), "cost_usd": round(compute_cost(plan_usage), 6)} if plan_usage else None,
    })

    # Answer each subquestion: patient = stub; non_patient = RAG + LLM (emit progress); collect usage and sources
    rag_filters = _rag_filters_from_state(new_state)
    _payer = (new_state.get("active") or {}).get("payer")
    _payers = (new_state.get("active") or {}).get("payers") or []
    if _payer or _payers or rag_filters:
        logger.info("RAG filters from state: active.payer=%s active.payers=%s filters=%s", _payer, _payers or None, rag_filters)
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
            rag_filters=rag_filters,
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
        agent_cards.append({
            "stage": agent_name,
            "sq_id": sq.id,
            "input_lines": in_lines,
            "output_lines": out_lines,
            "duration_ms": int(elapsed_ms),
            "usage": {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"), "cost_usd": round(compute_cost(usage), 6)} if usage else None,
        })

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
    agent_cards.append({
        "stage": "Integrator",
        "input_lines": integ_in,
        "output_lines": integ_out,
        "duration_ms": int(elapsed_integ_ms),
        "usage": {"input_tokens": integrator_usage.get("input_tokens"), "output_tokens": integrator_usage.get("output_tokens"), "cost_usd": round(compute_cost(integrator_usage), 6)} if integrator_usage else None,
    })
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

    # Confidence strip: single value for UI (worker classifies; frontend renders locked copy)
    source_confidence_strip = _derive_source_confidence_strip(all_sources)

    # Source cards for chat (from RAG; not dependent on integrator message)
    response_sources = [
        {
            "index": s.get("index", i + 1),
            "document_id": s.get("document_id"),
            "document_name": s.get("document_name") or "document",
            "page_number": s.get("page_number"),
            "source_type": s.get("source_type"),
            "document_authority_level": s.get("document_authority_level"),
            "match_score": s.get("match_score"),
            "confidence": s.get("confidence"),
            "text": (s.get("text") or "")[:200],
        }
        for i, s in enumerate(all_sources)
    ]

    duration_ms = int((time.perf_counter() - t0_total) * 1000)
    llm_provider = (usages[0].get("provider") or None) if usages else None
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None

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
        "thread_id": thread_id,
    }
    # Publish to Redis first so frontend/API get the response even if worker is killed or stream drops
    get_queue().publish_response(correlation_id, response_payload)
    logger.info("Response published for %s", correlation_id)
    clear_progress(correlation_id)
    store_response(correlation_id, response_payload)

    def _persist_response() -> None:
        try:
            append_assistant_message(thread_id, correlation_id, final_message)
            parsed_answer_card: dict[str, Any] | None = None
            try:
                parsed = json.loads(final_message)
                if isinstance(parsed, dict) and parsed.get("mode") and parsed.get("direct_answer") is not None and isinstance(parsed.get("sections"), list):
                    parsed_answer_card = parsed
            except (json.JSONDecodeError, TypeError):
                pass
            if parsed_answer_card is not None:
                slots = answer_card_to_open_slots(parsed_answer_card)
                register_open_slots(thread_id, slots)
                if not slots and (parsed_answer_card.get("mode") or "").upper() == "FACTUAL":
                    save_state(thread_id, {"turn_count_since_active_set": 0})
            insert_turn(
                correlation_id=correlation_id,
                question=message,
                thinking_log=thinking_chunks,
                final_message=final_message,
                sources=response_sources,
                duration_ms=duration_ms,
                model_used=model_used,
                llm_provider=llm_provider,
                session_id=session_id,
                thread_id=thread_id,
                plan_snapshot=plan_snapshot,
                blueprint_snapshot=blueprint_snapshot,
                agent_cards=agent_cards,
                source_confidence_strip=source_confidence_strip,
                config_sha=config_sha,
            )
        except Exception as e:
            logger.warning("Post-response persistence failed for %s: %s", correlation_id, e)

    threading.Thread(
        target=_persist_response,
        daemon=True,
        name=f"persist-{correlation_id[:8]}",
    ).start()


def run_one_turn_test(message: str) -> dict[str, Any]:
    """Run one pipeline pass with current config without persistence (no DB, no queue).
    Returns dict with reply, config_sha, model_used, duration_ms for config test endpoint."""
    t0 = time.perf_counter()
    config_sha = get_config_sha() or ""
    message = (message or "").strip() or "What is prior authorization?"
    state = copy.deepcopy(DEFAULT_STATE)
    last_turns: list[dict[str, Any]] = []
    patch, reset_reason = extract_state_patch(message, state, None, None)
    new_state = _merge_state(state, patch)
    route = route_context(message, new_state, last_turns, reset_reason)
    pack = build_context_pack(route, new_state, last_turns, new_state.get("open_slots", []))
    message_for_parser = (pack + "\n\n" + message) if pack else message

    def noop(_: str) -> None:
        pass

    plan = parse(message_for_parser, thinking_emitter=noop)
    rag_k_default = get_chat_config().rag.top_k
    blueprint = build_blueprint(plan, rag_default_k=rag_k_default)
    rag_filters = _rag_filters_from_state(new_state)
    answers: list[str] = []
    usages: list[LLMUsageDict] = []
    if getattr(plan, "llm_usage", None):
        usages.append(plan.llm_usage)
    for i, sq in enumerate(plan.subquestions):
        bp = blueprint[i] if i < len(blueprint) else {}
        retrieval_params = None
        if sq.kind == "non_patient":
            score = getattr(sq, "intent_score", None)
            if score is None:
                score = intent_to_score(getattr(sq, "question_intent", None))
            retrieval_params = get_retrieval_blend(score)
        ans, usage, _ = _answer_for_subquestion(
            sq.id, sq.kind, sq.text,
            retrieval_params=retrieval_params,
            emitter=noop,
            rag_filters=rag_filters,
        )
        answers.append(ans)
        if usage:
            usages.append(usage)
    final_message, integrator_usage = format_response(
        plan, answers, user_message=message, emitter=noop, message_chunk_callback=None
    )
    if integrator_usage:
        usages.append(integrator_usage)
    model_used = (usages[0].get("model") or None) if usages else None
    duration_ms = int((time.perf_counter() - t0) * 1000)

    # Per-stage outputs for config test UI (Planner, RAG answers, Integrator, Final)
    planner_output: dict[str, Any] = plan.model_dump()
    rag_answers_list: list[dict[str, Any]] = []
    for sq, ans in zip(plan.subquestions, answers):
        rag_answers_list.append({
            "sq_id": sq.id,
            "kind": sq.kind,
            "text": (sq.text or "")[:300] + ("..." if len(sq.text or "") > 300 else ""),
            "answer_preview": (ans or "")[:600] + ("..." if len(ans or "") > 600 else ""),
        })
    final_answer = final_message
    try:
        parsed = json.loads(final_message)
        if isinstance(parsed, dict) and parsed.get("direct_answer") is not None:
            final_answer = parsed.get("direct_answer") or final_message
    except (json.JSONDecodeError, TypeError):
        pass

    return {
        "reply": final_message,
        "config_sha": config_sha,
        "model_used": model_used,
        "duration_ms": duration_ms,
        "stages": {
            "planner": planner_output,
            "rag_answers": rag_answers_list,
            "integrator_raw": final_message,
            "final_answer": final_answer,
        },
    }


def run_worker() -> None:
    """Blocking: consume requests and process each."""
    from app.config import get_config
    cfg = get_config()
    logger.info("run_worker starting queue_type=%s", cfg.queue_type)
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
