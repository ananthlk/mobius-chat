"""Non-patient module: Vertex AI Vector Search + Postgres published_rag_metadata (1536 dims).
Embed question, search Vertex (top k + filters), fetch metadata from Postgres by id, pass context to LLM, return answer + sources + usage.
"""
import asyncio
import logging
from typing import Any

from app.trace_log import trace_entered

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
    filter_payer: str | list[str] | None = None,
    filter_state: str | None = None,
) -> tuple[str, list[dict], dict[str, Any] | None]:
    """Answer a non-patient subquestion: RAG (blend of hierarchical + factual or single path) then LLM. Returns (answer_text, sources, llm_usage).
    Per-request filter_payer/filter_state (e.g. from thread state) scope retrieval to that payer/state; overrides config."""
    trace_entered("services.non_patient_rag.answer_non_patient")
    from app.chat_config import get_chat_config
    from app.services.llm_provider import get_llm_provider

    cfg = get_chat_config()
    rag = cfg.rag
    use_blend = (n_hierarchical is not None and n_hierarchical > 0) or (n_factual is not None and n_factual > 0)
    if use_blend and (n_hierarchical or 0) == 0 and (n_factual or 0) == 0:
        use_blend = False
        k = k if k is not None else rag.top_k

    chunks: list[dict] = []
    if rag.vertex_index_endpoint_id and rag.vertex_deployed_index_id and rag.database_url:
        try:
            if use_blend:
                from app.services.published_rag_search import retrieve_with_blend
                chunks = retrieve_with_blend(
                    question,
                    n_hierarchical=n_hierarchical or 0,
                    n_factual=n_factual or 0,
                    confidence_min=confidence_min,
                    emitter=emitter,
                    filter_payer=filter_payer,
                    filter_state=filter_state,
                )
            else:
                from app.services.published_rag_search import search_published_rag
                k = k if k is not None else rag.top_k
                _emit(emitter, f"Searching our materials (up to {k} results)...")
                chunks = search_published_rag(
                    question, k=k, confidence_min=confidence_min, emitter=emitter,
                    filter_payer=filter_payer, filter_state=filter_state,
                )
                if chunks:
                    _emit(emitter, f"Using {len(chunks)} result{'s' if len(chunks) != 1 else ''} to answer this part.")
            if not chunks:
                _emit(emitter, "I didn’t find anything specific; I’ll answer from what I know.")
        except Exception as e:
            logger.warning("Published RAG search failed: %s", e)
            _emit(emitter, f"Search didn’t work ({e}). Answering without our materials.")
    else:
        _emit(emitter, "I don’t have access to our materials right now; I’ll answer from what I know.")
        missing = [x for x, v in [
            ("VERTEX_INDEX_ENDPOINT_ID", rag.vertex_index_endpoint_id),
            ("VERTEX_DEPLOYED_INDEX_ID", rag.vertex_deployed_index_id),
            ("CHAT_RAG_DATABASE_URL", rag.database_url),
        ] if not (v or "").strip()]
        logger.info(
            "RAG skipped: set %s in mobius-config/.env or mobius-chat/.env (see .env.example). See docs/PUBLISHED_RAG_SETUP.md.",
            ", ".join(missing) or "vertex_index_endpoint_id, vertex_deployed_index_id, database_url",
        )

    # Build context string and sources list for citations (include match_score, confidence for chat source cards)
    context_parts = []
    sources: list[dict] = []
    for i, c in enumerate(chunks):
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
            "document_authority_level": c.get("document_authority_level"),
            "match_score": c.get("match_score"),
            "confidence": c.get("confidence"),
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

    return (full_message, sources, usage)
