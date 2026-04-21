"""Non-patient module: RAG (via API or inline) + LLM.

When RAG_API_URL is set: calls RAG API (retrieve → rerank → assemble).
Else: mobius-retriever inline + doc_assembly.
"""
import os
import logging
from typing import Any

logger = logging.getLogger(__name__)
_DEBUG_RAG = os.environ.get("DEBUG_RAG", "1").lower() in ("1", "true", "yes")


def _debug_chunks(label: str, chunks: Any, max_items: int = 5) -> None:
    """Log type/structure of chunks for debugging list/get errors."""
    if not _DEBUG_RAG:
        return
    tc = type(chunks).__name__
    try:
        ln = len(chunks) if chunks is not None else 0
    except (TypeError, AttributeError):
        ln = "?"
    logger.info("[DEBUG_RAG] %s: type=%s len=%s", label, tc, ln)
    if chunks is None or ln == 0:
        return
    try:
        for i, c in enumerate(chunks):
            if i >= max_items:
                logger.info("[DEBUG_RAG]   ... and %s more", ln - max_items)
                break
            t = type(c).__name__
            h = ""
            if isinstance(c, dict):
                h = str(list(c.keys())[:8])
            elif isinstance(c, (list, tuple)):
                h = f"len={len(c)} first_type={type(c[0]).__name__ if c else 'n/a'}"
            logger.info("[DEBUG_RAG]   [%s] type=%s %s", i, t, h)
    except Exception as e:
        logger.warning("[DEBUG_RAG] %s iteration failed: %s", label, e)


def _emit(emitter, chunk: str) -> None:
    try:
        if emitter and chunk and str(chunk).strip():
            emitter(str(chunk).strip())
    except Exception as e:
        logger.debug("Emit failed (non-fatal): %s", e)


# Phase 0.18 — confidence-label → numeric fallback mapping.
# Values chosen so the default ``confidence_min=0.5`` admits useful chunks
# and excludes abstain-tier content. Tune here if label semantics change.
#
# The canonical labels set by ``doc_assembly.assign_confidence_*`` are:
#   "abstain"              — rerank_score < cfg.confidence_abstain_max (0.5)
#   "process_with_caution" — middle band (~0.5–0.75)
#   "process_confident"    — rerank_score >= cfg.confidence_process_confident_min
# Those are the ones that matter for the RAG API path. Other labels here
# are defensive defaults for other pipelines (Vertex-direct, google-only)
# that have emitted different labels historically.
_CONFIDENCE_LABEL_SCORE: dict[str, float] = {
    # Canonical doc_assembly labels (the ones that actually flow through):
    "process_confident":      0.9,
    "process_with_caution":   0.55,
    "abstain":                0.3,   # intentionally below default 0.5 threshold
    # Historical / alternative label spellings — keep for back-compat:
    "approved_authoritative": 1.0,
    "authoritative":          1.0,
    "approved_informational": 0.8,
    "informational":          0.8,
    "high":                   0.9,
    "medium":                 0.6,
    "proceed_with_caution":   0.55,  # alternate spelling
    "low":                    0.3,
    "augmented_with_google":  0.5,
}


def _score_chunk_for_confidence_filter(c: dict) -> float:
    """Return a 0.0–1.0 numeric score usable by ``confidence_min`` filtering.

    Works for both chunk shapes the retrieval layer produces:

    - **Inline BM25** — keys include ``match_score`` (normalized BM25 sigmoid) and/or
      ``confidence`` (numeric). Pre-0.18 filter only looked at these.
    - **RAG API** — keys include ``rerank_score`` (numeric) and ``confidence_label``
      (string: "high" / "informational" / etc.). Pre-0.18 filter missed both and
      treated every RAG-API chunk as 0.0 confidence → silently filtered the entire
      corpus answer out of the ReAct turn.

    Lookup order: ``match_score`` → ``confidence`` → ``rerank_score`` →
    ``confidence_label`` (via ``_CONFIDENCE_LABEL_SCORE``). First numeric hit
    wins. Non-numeric / unknown values fall through.
    """
    for field in ("match_score", "confidence", "rerank_score"):
        v = c.get(field)
        if isinstance(v, (int, float)) and v >= 0:
            return float(v)
    lbl = (c.get("confidence_label") or "").strip().lower()
    return _CONFIDENCE_LABEL_SCORE.get(lbl, 0.0)


def answer_non_patient(
    question: str,
    k: int | None = None,
    confidence_min: float | None = None,
    n_hierarchical: int | None = None,
    n_factual: int | None = None,
    emitter=None,
    correlation_id: str | None = None,
    subquestion_id: str | None = None,
    rag_filter_overrides: dict[str, str] | None = None,
    include_document_ids: list[str] | None = None,
    on_rag_fail: list[str] | None = None,
    thread_id: str | None = None,
    phi_detected: bool = False,
    config_sha: str | None = None,
    mode: str | None = None,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Answer a non-patient subquestion: RAG (blend of hierarchical + factual or single path) then LLM.
    Returns (answer_text, sources, llm_usage, retrieval_signal). retrieval_signal: corpus_only | corpus_plus_google | google_only | no_sources."""
    from app.chat_config import get_chat_config
    from app.services.doc_assembly import RETRIEVAL_SIGNAL_NO_SOURCES
    from app.services.retrieval_emit_adapter import wrap_emitter_for_user

    cfg = get_chat_config()
    rag = cfg.rag
    overrides = rag_filter_overrides if isinstance(rag_filter_overrides, dict) else {}
    fp = overrides.get("filter_payer") if overrides else None
    fst = overrides.get("filter_state") if overrides else None
    fpr = overrides.get("filter_program") if overrides else None
    use_blend = (n_hierarchical is not None and n_hierarchical > 0) or (n_factual is not None and n_factual > 0)
    if use_blend and (n_hierarchical or 0) == 0 and (n_factual or 0) == 0:
        use_blend = False
        k = k if k is not None else rag.top_k

    chunks: list[dict] = []
    retrieval_signal = RETRIEVAL_SIGNAL_NO_SOURCES
    retrieval_trace: dict | None = None
    rag_api_url = (os.environ.get("RAG_API_URL") or "").strip()
    include_trace = bool(correlation_id and subquestion_id and rag_api_url)

    if rag.database_url:
        try:
            from app.services.retriever_backend import retrieve_for_chat
            # Normalize DSN the same way db_client does so psycopg2 inside
            # retriever_backend gets a clean URL (no ``+psycopg2`` driver
            # prefix) with CHAT_DB_PASSWORD from Secret Manager injected.
            # Without this, Cloud Run logs ``BM25 corpus fetch failed:
            # invalid dsn: invalid connection option "postgresql+psycopg2"``.
            from app.db_client import _get_fallback_url
            retrieval_db_url = _get_fallback_url("chat") or rag.database_url
            k = k if k is not None else rag.top_k
            total_k = max(k, (n_hierarchical or 0) + (n_factual or 0)) if use_blend else k
            chunks, retrieval_trace = retrieve_for_chat(
                question,
                top_k=total_k,
                database_url=retrieval_db_url,
                filter_payer=(fp if fp is not None else rag.filter_payer) or "",
                filter_state=(fst if fst is not None else rag.filter_state) or "",
                filter_program=(fpr if fpr is not None else rag.filter_program) or "",
                filter_authority_level=rag.filter_authority_level or "",
                n_factual=n_factual,
                n_hierarchical=n_hierarchical,
                emitter=emitter,
                include_trace=include_trace,
                include_document_ids=include_document_ids,
            )
            if retrieval_trace is not None and not isinstance(retrieval_trace, dict):
                logger.warning("[DEBUG_RAG] retrieval_trace is %s not dict, ignoring", type(retrieval_trace).__name__)
                retrieval_trace = None
            if not isinstance(chunks, list):
                logger.warning("[DEBUG_RAG] chunks is %s not list, using []", type(chunks).__name__)
                chunks = []
            _debug_chunks("after retrieve_for_chat", chunks)
            # Defensive: keep only dict-like chunks (handles list/Row from API or DB)
            _normalized: list[dict] = []
            for i, c in enumerate(chunks):
                try:
                    if _DEBUG_RAG and i < 3:
                        logger.info("[DEBUG_RAG] normalize chunk[%s] type=%s", i, type(c).__name__)
                    if isinstance(c, dict):
                        _normalized.append(dict(c))
                    elif isinstance(c, (list, tuple)) and c:
                        if _DEBUG_RAG and i < 2:
                            f0 = c[0] if c else None
                            logger.info("[DEBUG_RAG] chunk[%s] is list-like first_el type=%s", i, type(f0).__name__ if f0 is not None else "None")
                        if all(isinstance(x, (list, tuple)) and len(x) == 2 for x in c):
                            _normalized.append(dict(c))
                except (TypeError, AttributeError, ValueError) as ex:
                    logger.warning("[DEBUG_RAG] chunk[%s] skip: %s (type=%s)", i, ex, type(c).__name__)
                    continue
            chunks = _normalized
            _debug_chunks("after normalize", chunks)
            if confidence_min is not None and chunks:
                # Phase 0.18: when the RAG API path is active, chunks have
                # ``rerank_score`` + ``confidence_label`` but no ``match_score``
                # or ``confidence``. ``_score_chunk_for_confidence_filter``
                # falls through numeric fields and then a label→numeric map.
                _pre_filter = len(chunks)
                _scored: list[tuple[dict, float]] = []
                for c in chunks:
                    if not isinstance(c, dict):
                        continue
                    s = _score_chunk_for_confidence_filter(c)
                    _scored.append((c, s))
                chunks = [c for c, s in _scored if s >= confidence_min]
                if _DEBUG_RAG:
                    # Log what actually got dropped so invisible retrieval-kills
                    # (the 2026-04-17 bug) stay visible going forward.
                    dropped = [(s, (c.get("confidence_label") or ""), c.get("rerank_score")) for c, s in _scored if s < confidence_min]
                    kept = [(s, (c.get("confidence_label") or ""), c.get("rerank_score")) for c, s in _scored if s >= confidence_min]
                    logger.info(
                        "[DEBUG_RAG] confidence_min=%.2f filter: %d → %d (kept=%s dropped=%s)",
                        confidence_min, _pre_filter, len(chunks),
                        kept[:5], dropped[:5],
                    )
            if not chunks:
                _emit(emitter, "I didn't find anything specific; I'll answer from what I know.")
                if on_rag_fail and "search_google" in [str(x).lower() for x in on_rag_fail]:
                    try:
                        from app.services.doc_assembly import google_search_via_skills_api
                        from app.services.doc_assembly import RETRIEVAL_SIGNAL_GOOGLE_ONLY
                        google_results = google_search_via_skills_api(question)
                        if google_results:
                            chunks = google_results
                            retrieval_signal = RETRIEVAL_SIGNAL_GOOGLE_ONLY
                            _emit(emitter, "I'm adding external search results to help answer.")
                    except Exception as eg:
                        logger.debug("Google fallback failed: %s", eg)
            if retrieval_trace and correlation_id and subquestion_id:
                try:
                    from app.storage.retrieval_persistence import insert_retrieval_run
                    insert_retrieval_run(
                        correlation_id=correlation_id,
                        subquestion_id=subquestion_id,
                        subquestion_text=question,
                        path=(os.environ.get("RAG_PATH") or "mobius").strip().lower() or "mobius",
                        n_factual=n_factual,
                        n_hierarchical=n_hierarchical,
                        trace=retrieval_trace,
                        assembled=chunks,
                    )
                except Exception as ep:
                    logger.debug("Retrieval persistence failed: %s", ep)
        except Exception as e:
            from app.communication.error_emit import classify_exception
            env = classify_exception(e, tool="search_corpus")
            logger.warning(
                "Retrieval failed [%s]: %s", env.error_code, env.internal_detail, exc_info=True
            )
            _emit(emitter, f"{env.user_facing_message} Answering without our materials.")
    else:
        _emit(emitter, "I don’t have access to our materials right now; I’ll answer from what I know.")
        logger.info("RAG: database_url not set; skipping RAG")

    # Doc assembly: RAG API returns chunks that are already assembled (have confidence_label).
    # Inline BM25 fallback chunks lack confidence_label and need assembly to:
    #   (a) apply blend selection (n_hierarchical paragraphs + n_factual sentences)
    #   (b) assign confidence labels
    #   (c) optionally apply Google fallback
    # Detect inline BM25 chunks by absence of confidence_label on any returned chunk.
    _chunks_from_api = bool(
        rag_api_url and chunks and any(c.get("confidence_label") for c in chunks[:5])
    )
    if chunks and not _chunks_from_api:
        try:
            from app.services.doc_assembly import assemble_docs
            chunks, retrieval_signal = assemble_docs(
                chunks,
                question,
                apply_google=True,
                expand_neighbors=True,
                database_url=rag.database_url if rag else None,
                emitter=wrap_emitter_for_user(emitter),
            )
        except Exception as e:
            logger.warning("Doc assembly failed: %s; using raw chunks", e)

    # Build context string and sources list for citations (include match_score, confidence, confidence_label)
    from app.services.doc_assembly import _ensure_chunk_dict

    _debug_chunks("before context build", chunks)
    context_parts = []
    sources: list[dict] = []
    for i, c in enumerate(chunks):
        if _DEBUG_RAG and i < 2:
            logger.info("[DEBUG_RAG] context loop i=%s type=%s", i, type(c).__name__)
        try:
            c = _ensure_chunk_dict(c)
        except (TypeError, AttributeError, ValueError):
            logger.warning("Chunk[%s] invalid (type=%s), skipping", i, type(c).__name__)
            continue
        text = c.get("text") or ""
        if not text:
            continue
        doc_name = c.get("document_name") or c.get("document_id") or "document"
        page = c.get("page_number")
        source_type = c.get("source_type") or "chunk"
        context_parts.append(f"[{i + 1}] {text}")
        sources.append({
            "index": i + 1,
            "text": text[:300] + "..." if len(text) > 300 else text,
            "document_id": c.get("document_id"),
            "document_name": doc_name,
            "page_number": page,
            "source_type": source_type,
            "match_score": c.get("match_score"),
            "confidence": c.get("confidence"),
            "rerank_score": c.get("rerank_score"),
            "confidence_label": c.get("confidence_label"),
            "llm_guidance": c.get("llm_guidance"),
            "distance": c.get("distance"),
        })
    context = "\n\n".join(context_parts) if context_parts else "(No retrieved context.)"

    # Prepend jurisdiction scope so the LLM knows docs are pre-filtered for that payer/state
    jurisdiction_summary = None
    if overrides and (fp or fst or fpr):
        from app.state.jurisdiction import jurisdiction_to_summary
        j = {"payor": (fp or "").strip(), "state": (fst or "").strip(), "program": (fpr or "").strip()}
        jurisdiction_summary = jurisdiction_to_summary(j)
    if jurisdiction_summary and context_parts:
        context = (
            f"Scope: The documents below were pre-filtered for {jurisdiction_summary}. "
            "They are from that payer's materials. Use them to answer—do not say the context lacks information about "
            f"{jurisdiction_summary}; the documents are already scoped to that payer.\n\n"
            + context
        )

    # Call LLM with context + question (ModelRouter stage `rag` → llm_calls + rotation)
    usage: dict[str, Any] | None = None
    try:
        from app.services.llm_manager import generate_sync

        try:
            max_rag_tokens = max(
                256,
                min(65536, int(os.environ.get("CHAT_RAG_ANSWER_MAX_TOKENS", "8192"))),
            )
        except ValueError:
            max_rag_tokens = 8192
        template = cfg.prompts.rag_answering_user_template
        prompt = template.format(context=context, question=question)
        answer, usage = generate_sync(
            prompt,
            stage="rag",
            max_tokens=max_rag_tokens,
            config_sha=config_sha,
            correlation_id=correlation_id,
            thread_id=thread_id,
            phi_detected=phi_detected,
            mode=mode,
        )
    except Exception as e:
        from app.communication.error_emit import classify_exception
        env = classify_exception(e, tool="non_patient_rag_llm")
        logger.warning(
            "Non-patient LLM failed [%s]: %s", env.error_code, env.internal_detail
        )
        # ``answer`` goes into downstream formatting; keep it short and clean.
        # It is NOT a user-facing bubble on its own — still gate it behind the envelope.
        answer = f"[{env.error_code}]"
        _emit(emitter, f"I couldn’t answer this part — {env.user_facing_message.lower()}")

    # Format response: answer + sources section
    if sources:
        lines = [answer.strip(), "", "Sources:"]
        for s in sources:
            idx = s.get("index", 0)
            doc_name = s.get("document_name") or "document"
            page = s.get("page_number")
            cite = f"  [{idx}] {doc_name}"
            if page is not None:
                cite += f" (page {page})"
            cite += f" — {s.get('text', '')[:120]}..."
            lines.append(cite)
        full_message = "\n".join(lines)
    else:
        full_message = answer.strip()

    # When we have corpus chunks but didn't go through assemble_docs, infer signal
    from app.services.doc_assembly import RETRIEVAL_SIGNAL_CORPUS_ONLY
    if chunks and retrieval_signal == RETRIEVAL_SIGNAL_NO_SOURCES:
        retrieval_signal = RETRIEVAL_SIGNAL_CORPUS_ONLY

    return (full_message, sources, usage, retrieval_signal)
