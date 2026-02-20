"""Non-patient module: RAG (via API or inline) + LLM.

When RAG_API_URL is set: calls RAG API (retrieve → rerank → assemble).
Else: mobius-retriever inline + doc_assembly.
"""
import asyncio
import os
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _emit(emitter, chunk: str) -> None:
    if emitter and chunk.strip():
        emitter(chunk.strip())


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

    cfg = get_chat_config()
    rag = cfg.rag
    overrides = rag_filter_overrides or {}
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
            if confidence_min is not None and chunks:
                chunks = [c for c in chunks if (c.get("match_score") or c.get("confidence") or 0.0) >= confidence_min]
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
                emitter=emitter,
            )
        except Exception as e:
            logger.warning("Doc assembly failed: %s; using raw chunks", e)

    # Build context string and sources list for citations (include match_score, confidence, confidence_label)
    from app.services.doc_assembly import _ensure_chunk_dict

    context_parts = []
    sources: list[dict] = []
    for i, c in enumerate(chunks):
        c = _ensure_chunk_dict(c)
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
