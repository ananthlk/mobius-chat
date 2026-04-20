"""Instant-RAG lazy search (Phase B.1).

Dedicated retrieval path for user-uploaded documents — the "lazy RAG"
pattern the user described: *just match on vector ID and pass chunks*,
no tag-matching, no confidence filters, no LLM synthesis.

Why this module exists instead of reusing ``answer_non_patient``
----------------------------------------------------------------
The main RAG pipeline runs a layered filter chain designed for the
curated corpus:

  J/P/D tagger → BM25 retrieval → tag-match rerank →
  confidence_min filter → doc_assembly neighbor expansion → LLM synthesis

User uploads don't have *any* of those upstream artifacts:

  * ``document_tags`` row: does NOT exist (populated by dbt batch only).
  * ``policy_line_tags`` rows: do NOT exist (populated by dbt batch only).
  * ``confidence_label`` per chunk: absent.
  * Curated payer / state / program metadata: empty unless the upload
    promotion flow runs (Phase B.7).

Running the chain against those empty fields produces two failure modes:
(a) tag-match rerank silently down-ranks the upload, and (b) the
confidence filter can drop everything if labels are missing. The user
flagged exactly this when they said "it may not have all the tags... just
match on vector ID and pass chunks — easier."

So this module:

  1. Embeds the query once.
  2. Vector-searches Chroma filtered to ``document_id`` (+ ``instant_rag=true``
     as a belt-and-suspenders check).
  3. Returns chunks in the same tuple shape ``answer_non_patient`` uses so
     the ReAct loop's dispatch branch downstream can treat the two tools
     identically. **No LLM synthesis step** — the integrator at the end of
     the turn handles synthesis, and skipping it here saves a whole round
     trip per tool call.

If the upload gets promoted (Phase B.7), those chunks graduate into the
main corpus with real J/P/D tags and ``search_corpus`` finds them
naturally; this path then becomes redundant for that doc_id but stays
live for the other still-ephemeral uploads.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


# Signal strings match the RETRIEVAL_SIGNAL_* constants in doc_assembly.py.
# Duplicated here to avoid importing doc_assembly (which pulls in the whole
# main retrieval stack) just for two string constants.
_SIGNAL_NO_SOURCES = "no_sources"
_SIGNAL_CORPUS_ONLY = "corpus_only"  # reused: "we answered from retrieved chunks"


def lazy_rag_search(
    document_id: str,
    question: str,
    k: int = 8,
    *,
    emitter: Callable[[str], None] | None = None,
) -> tuple[str, list[dict[str, Any]], dict | None, str]:
    """Vector-search a single uploaded document and return its top-k chunks.

    Return tuple matches ``answer_non_patient(...)`` so callers can swap
    between the two tools without shape changes:

        (answer_text, sources, usage, signal)

    skills-core refactor (Day 5, 2026-04-20):
    Previously this function did embed + Chroma query + metadata
    extraction inline. Now it's a thin adapter over
    ``mobius_skills_core.skills.thread_corpus_search.run_thread_corpus_search``
    — the shared skill is the single source of truth for this
    retrieval pattern, shared with external MCP consumers.

    The chat-specific pieces that stay here:
      * config read (chat_config.get_chat_config())
      * embedding provider (chat's get_query_embedding)
      * legacy string emits for pre-envelope callers
      * tuple return shape the ReAct loop expects

    All the Chroma + filter + scoring logic now lives in the shared
    package. Preserves the (answer, sources, usage, signal) tuple so
    downstream (react_loop's tool dispatch) works unchanged.
    """
    if not document_id or not document_id.strip():
        return ("", [], None, _SIGNAL_NO_SOURCES)
    if not question or not question.strip():
        return ("", [], None, _SIGNAL_NO_SOURCES)

    try:
        from app.chat_config import get_chat_config
        from app.services.embedding_provider import get_query_embedding
        from mobius_skills_core.skills.corpus_search import ChromaConfig
        from mobius_skills_core.skills.thread_corpus_search import (
            run_thread_corpus_search,
        )
    except ImportError as e:
        logger.warning("instant-rag: required deps missing: %s", e)
        return ("", [], None, _SIGNAL_NO_SOURCES)

    cfg = get_chat_config()
    rag = cfg.rag
    if not rag.chroma_persist_dir:
        logger.warning("instant-rag: CHROMA_PERSIST_DIR not configured")
        return ("", [], None, _SIGNAL_NO_SOURCES)

    _emit(emitter, "Reading your attached document…")

    result = run_thread_corpus_search(
        document_id=document_id,
        question=question,
        embed_query=get_query_embedding,
        chroma=ChromaConfig(
            persist_dir=rag.chroma_persist_dir,
            collection=rag.chroma_collection or "published_rag",
        ),
        k=k,
        # emitter deliberately None — legacy string emit above covers
        # the pre-envelope UI surface; SkillEvent → EmitEnvelope
        # translation comes in a follow-up.
    )

    if result.signal != "ok":
        # Surface the diagnostic hint from the shared skill when
        # present — the vector_count_hint tells operators whether the
        # document's chunks are missing from Chroma (ingest gap) vs.
        # present but the query embedding missed them (similarity).
        hint = (result.extra or {}).get("vector_count_hint")
        if hint is not None:
            logger.warning(
                "[instant-rag] empty Chroma result for document_id=%r. "
                "vector_count_hint=%s (0 = nothing indexed for this doc; "
                ">0 = query embedding missed).",
                document_id, hint,
            )
        _emit(emitter, "  ↓ your attached document doesn't cover this.")
        return ("", [], None, _SIGNAL_NO_SOURCES)

    # Convert SkillResult.chunks → legacy dict shape the chat integrator
    # knows how to cite. Preserves the pre-refactor field names
    # (rerank_score, instant_rag flag, etc.) so downstream integrator +
    # retry-guard code works unchanged.
    sources: list[dict[str, Any]] = []
    for idx, chunk in enumerate(result.chunks, 1):
        md = chunk.metadata or {}
        sources.append({
            "id": chunk.chunk_id,
            "text": chunk.text,
            "document_id": chunk.document_id or document_id,
            "document_name": chunk.document_name,
            "page_number": chunk.page_number,
            "source_type": md.get("source_type") or "instant_rag",
            "rerank_score": chunk.score,
            # No confidence_label — _score_chunk_for_confidence_filter
            # (Phase 0.18) falls through to rerank_score.
            "instant_rag": True,
        })

    _emit(
        emitter,
        f"  ✓ found {len(sources)} passage{'s' if len(sources) != 1 else ''} in your attached document.",
    )

    return (result.text, sources, None, _SIGNAL_CORPUS_ONLY)


def _emit(emitter: Callable[[str], None] | None, line: str) -> None:
    if emitter and line.strip():
        try:
            emitter(line.strip())
        except Exception:
            # Don't let UI emit failures break retrieval.
            pass
