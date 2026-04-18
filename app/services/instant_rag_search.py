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

    - ``answer_text``: the chunk texts joined with section-break markers.
      **Not** an LLM synthesis — the ReAct integrator does that once at
      the end of the turn. Joining raw chunks keeps this tool cheap
      (no synth LLM call, no 2-5s latency per dispatch).
    - ``sources``: the per-chunk dicts the downstream integrator already
      knows how to cite (text, document_name, page_number, rerank_score).
    - ``usage``: None — we do not burn LLM tokens here. Embedding cost is
      tracked separately by the embedding provider if it emits usage.
    - ``signal``: ``no_sources`` when zero chunks come back (so the ReAct
      retry guard records a failed attempt), else ``corpus_only`` so the
      integrator treats these chunks the same as a ``search_corpus`` hit.
    """
    if not document_id or not document_id.strip():
        return ("", [], None, _SIGNAL_NO_SOURCES)
    if not question or not question.strip():
        return ("", [], None, _SIGNAL_NO_SOURCES)

    # Lazy imports so loading this module doesn't pull the embedding /
    # chroma deps if nothing calls the function.
    try:
        from app.chat_config import get_chat_config
        from app.services.embedding_provider import get_query_embedding
    except ImportError as e:
        logger.warning("instant-rag: embedding / config deps missing: %s", e)
        return ("", [], None, _SIGNAL_NO_SOURCES)

    cfg = get_chat_config()
    rag = cfg.rag
    if not rag.chroma_persist_dir:
        logger.warning("instant-rag: CHROMA_PERSIST_DIR not configured")
        return ("", [], None, _SIGNAL_NO_SOURCES)

    _emit(emitter, f"Searching your uploaded doc (id={document_id[:8]}…)")

    try:
        query_embedding = get_query_embedding(question)
    except Exception as e:
        # Embedding provider errors shouldn't take down the tool — report
        # no_sources so the retry guard records it and the planner pivots.
        logger.warning("instant-rag: embedding failed: %s", e)
        return ("", [], None, _SIGNAL_NO_SOURCES)

    try:
        import chromadb
    except ImportError:
        logger.warning("instant-rag: chromadb not installed")
        return ("", [], None, _SIGNAL_NO_SOURCES)

    try:
        client = chromadb.PersistentClient(path=rag.chroma_persist_dir)
        coll = client.get_or_create_collection(
            name=rag.chroma_collection,
            metadata={"hnsw:space": "cosine"},
        )
    except Exception as e:
        logger.warning("instant-rag: chroma open failed: %s", e)
        return ("", [], None, _SIGNAL_NO_SOURCES)

    # Scope strictly to this document + the instant_rag flag. The flag is
    # redundant given document_id, but keeping it catches corruption cases
    # where a chunk was mis-tagged.
    where = {
        "$and": [
            {"document_id": document_id},
            {"instant_rag": "true"},
        ]
    }

    try:
        result = coll.query(
            query_embeddings=[query_embedding],
            n_results=k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        logger.warning("instant-rag: chroma query failed: %s", e)
        return ("", [], None, _SIGNAL_NO_SOURCES)

    ids = (result.get("ids") or [[]])[0]
    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]

    if not ids:
        # 2026-04-17 diagnostic: empty Chroma result has two very different
        # causes — (a) no vectors for this document_id (ingest failed to
        # write to Chroma, e.g. dimension mismatch), (b) vectors exist but
        # none passed the similarity cutoff. Log enough to tell them apart.
        try:
            probe = coll.get(where={"document_id": document_id}, limit=1)
            vector_count_hint = len(probe.get("ids") or [])
        except Exception:
            vector_count_hint = -1
        logger.warning(
            "[instant-rag] empty Chroma result for document_id=%r. "
            "Vectors-for-doc probe: %s "
            "(0 = skill didn't write to Chroma — likely embedding dim mismatch "
            "or write error; >0 = query embedding missed all of them).",
            document_id,
            vector_count_hint if vector_count_hint >= 0 else "probe_failed",
        )
        _emit(emitter, "  ↓ nothing relevant found in the uploaded doc.")
        return ("", [], None, _SIGNAL_NO_SOURCES)

    # Distance is cosine in [0, 2]; invert to a 0..1 similarity score the
    # rest of the pipeline calls "rerank_score" so the integrator treats
    # these chunks like any other retrieved chunk.
    sources: list[dict[str, Any]] = []
    for cid, text, meta, dist in zip(ids, docs, metas, distances):
        if not text or not str(text).strip():
            continue
        m = meta or {}
        rerank_score = max(0.0, min(1.0, 1.0 - (float(dist or 0.0) / 2.0)))
        sources.append({
            "id": str(cid),
            "text": str(text),
            "document_id": str(m.get("document_id") or document_id),
            "document_name": str(m.get("display_name") or m.get("filename") or "Uploaded document"),
            "page_number": m.get("page_number"),
            "source_type": str(m.get("source_type") or "instant_rag"),
            "rerank_score": rerank_score,
            # No confidence_label — ``_score_chunk_for_confidence_filter``
            # (Phase 0.18) will fall through to the rerank_score above.
            "instant_rag": True,
        })

    if not sources:
        return ("", [], None, _SIGNAL_NO_SOURCES)

    # "answer" for the tool result is the raw chunk text joined with
    # separators so the integrator has enough to synthesize. We do NOT
    # run an LLM here; the end-of-turn integrator is the single synthesis
    # point per turn (keeps latency + cost predictable).
    answer = "\n\n---\n\n".join(s["text"] for s in sources)

    _emit(emitter, f"  ✓ matched {len(sources)} chunk(s) from the uploaded doc.")

    return (answer, sources, None, _SIGNAL_CORPUS_ONLY)


def _emit(emitter: Callable[[str], None] | None, line: str) -> None:
    if emitter and line.strip():
        try:
            emitter(line.strip())
        except Exception:
            # Don't let UI emit failures break retrieval.
            pass
