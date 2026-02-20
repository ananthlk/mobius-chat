"""Non-patient module: RAG (via API or inline) + LLM.

When RAG_API_URL is set: calls RAG API (retrieve → rerank → assemble).
Else: mobius-retriever inline + doc_assembly.
"""
import asyncio
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
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Answer a non-patient subquestion: RAG (blend of hierarchical + factual or single path) then LLM.
    Returns (answer_text, sources, llm_usage, retrieval_signal). retrieval_signal: corpus_only | corpus_plus_google | google_only | no_sources."""
    from app.chat_config import get_chat_config
    from app.services.doc_assembly import RETRIEVAL_SIGNAL_NO_SOURCES
    from app.services.llm_provider import get_llm_provider
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
            k = k if k is not None else rag.top_k
            total_k = max(k, (n_hierarchical or 0) + (n_factual or 0)) if use_blend else k
            chunks, retrieval_trace = retrieve_for_chat(
                question,
                top_k=total_k,
                database_url=rag.database_url,
                filter_payer=(fp if fp is not None else rag.filter_payer) or "",
                filter_state=(fst if fst is not None else rag.filter_state) or "",
                filter_program=(fpr if fpr is not None else rag.filter_program) or "",
                filter_authority_level=rag.filter_authority_level or "",
                n_factual=n_factual,
                n_hierarchical=n_hierarchical,
                emitter=emitter,
                include_trace=include_trace,
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
                chunks = [
                    c for c in chunks
                    if isinstance(c, dict) and (c.get("match_score") or c.get("confidence") or 0.0) >= confidence_min
                ]
            if not chunks:
                _emit(emitter, "I didn’t find anything specific; I’ll answer from what I know.")
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
            logger.warning("Retrieval failed: %s", e)
            _emit(emitter, f"Search didn’t work ({e}). Answering without our materials.")
    else:
        _emit(emitter, "I don’t have access to our materials right now; I’ll answer from what I know.")
        logger.info("RAG: database_url not set; skipping RAG")

    # Doc assembly: only when not using RAG API (API returns assembled docs)
    if chunks and not rag_api_url:
        try:
            from app.services.doc_assembly import assemble_docs
            chunks, retrieval_signal = assemble_docs(
                chunks,
                question,
                apply_google=True,
                expand_neighbors=False,
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

    # Call LLM with context + question
    _emit(emitter, "Reading what I found and writing an answer...")
    usage: dict[str, Any] | None = None
    try:
        provider = get_llm_provider()
        template = cfg.prompts.rag_answering_user_template
        prompt = template.format(context=context, question=question)
        answer, usage = asyncio.run(provider.generate_with_usage(prompt))
        _emit(emitter, "Done with this part.")
    except Exception as e:
        logger.warning("Non-patient LLM failed: %s", e)
        answer = f"[LLM failed: {e}]"
        _emit(emitter, f"I couldn’t answer this part: {e}.")

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
