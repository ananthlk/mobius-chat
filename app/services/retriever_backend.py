"""Adapter: mobius-retriever output → doc_assembly input format.

When RAG_API_URL is set: call RAG API (mobius or lazy path).
Else: inline BM25 → rerank (legacy).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Callable

from app.services.retrieval_emit_adapter import wrap_emitter_for_user

logger = logging.getLogger(__name__)
_DEBUG_RAG = os.environ.get("DEBUG_RAG", "1").lower() in ("1", "true", "yes")


def _debug_chunks(label: str, chunks: list, max_items: int = 3) -> None:
    if not _DEBUG_RAG or chunks is None:
        return
    try:
        logger.info("[DEBUG_RAG retriever] %s: len=%s", label, len(chunks))
        for i, c in enumerate(chunks[:max_items]):
            t = type(c).__name__
            logger.info("[DEBUG_RAG retriever]   [%s] type=%s", i, t)
    except Exception as e:
        logger.warning("[DEBUG_RAG retriever] %s failed: %s", label, e)


# Default reranker config path (same as path_b_v1)
_DEFAULT_RERANKER_CONFIG = "configs/reranker_v1.yaml"


def _emit(emitter: Callable[[str], None] | None, msg: str) -> None:
    if emitter and msg.strip():
        emitter(msg.strip())


def _bm25_to_rerank_dict(c: dict[str, Any], bm25_cfg: dict | None) -> dict[str, Any]:
    """Convert BM25 chunk to reranker input format with similarity = sigmoid(raw_score)."""
    raw = c.get("raw_score")
    pt = c.get("provision_type", "sentence")
    if raw is not None and bm25_cfg:
        from mobius_retriever.config import apply_normalize_bm25
        sim = apply_normalize_bm25(float(raw), pt, bm25_cfg)
    elif raw is not None:
        sim = min(1.0, float(raw) / 50.0)
    else:
        sim = c.get("similarity") or c.get("rerank_score") or 0.0
    retrieval_source = f"bm25_{pt}" if pt in ("paragraph", "sentence") else "bm25_sentence"
    return {
        "id": c.get("id"),
        "text": c.get("text") or "",
        "document_id": c.get("document_id"),
        "document_name": c.get("document_name") or "document",
        "document_authority_level": c.get("document_authority_level"),
        "page_number": c.get("page_number"),
        "similarity": sim,
        "raw_score": raw,
        "provision_type": pt,
        "source_type": c.get("source_type", "hierarchical"),
        "retrieval_source": retrieval_source,
    }


def _raw_to_chat_chunk(c: dict[str, Any], match_score: float | None) -> dict[str, Any]:
    """Convert retriever raw dict to chat/doc_assembly format."""
    pt = c.get("provision_type", "sentence")
    # Preserve retrieval_source so _is_sentence_level() works in assemble blend selection
    retrieval_source = c.get("retrieval_source") or f"bm25_{pt}"
    return {
        "id": c.get("id"),
        "text": c.get("text") or "",
        "document_id": c.get("document_id"),
        "document_name": c.get("document_name") or "document",
        "page_number": c.get("page_number"),
        "paragraph_index": c.get("paragraph_index"),
        "source_type": c.get("source_type") or "chunk",
        "document_authority_level": c.get("document_authority_level"),
        "match_score": match_score,
        "confidence": match_score,
        "rerank_score": c.get("rerank_score") or match_score,
        "raw_score": c.get("raw_score"),
        "provision_type": pt,
        "retrieval_source": retrieval_source,
    }


def retrieve_via_rag_api(
    question: str,
    path: str = "mobius",
    top_k: int = 10,
    apply_google: bool = True,
    n_factual: int | None = None,
    n_hierarchical: int | None = None,
    emitter: Callable[[str], None] | None = None,
    include_trace: bool = False,
    filter_payer: str = "",
    filter_state: str = "",
    filter_program: str = "",
    filter_authority_level: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Call RAG API. Returns (chunks, trace or None).

    2026-04-27: contract change. The mobius-rag service deprecated
    ``POST /retrieve`` (returned ``{docs, retrieval_trace}`` and accepted
    payer/state/program filters + factual/hierarchical knobs) in favor of
    a lightweight ``POST /api/query`` that takes ``{query, k}`` and
    returns ``{chunks: [ChunkOut, ...]}`` — embed query, vector search,
    resolve source_id to text. No filters, no trace.

    Symptom before this fix: every search_corpus call hit the dead
    ``/retrieve`` endpoint, got HTTP 405, and silently fell through to
    the inline-BM25 path. Result: corpus answers were running on a
    direct-DB BM25 with stale tagging instead of the RAG service.

    Args ``path``, ``apply_google``, ``include_trace``, ``n_factual``,
    ``n_hierarchical``, ``filter_*`` are accepted for caller stability
    but the new endpoint silently ignores all of them. Filters that
    used to narrow at retrieval time now have to be enforced upstream
    (curator / search_corpus filtering) or downstream (post-fetch
    filtering on the returned chunks). See note in caller for the
    transitional plan.
    """
    url = (os.environ.get("RAG_API_URL") or "").strip()
    if not url:
        return [], None
    base = url.rstrip("/")
    api_url = f"{base}/api/query"
    # New contract: {query, k} only. Field renames + everything else dropped.
    payload_obj: dict = {
        "query": question,
        "k": int(top_k) if top_k else 10,
    }
    payload = json.dumps(payload_obj).encode("utf-8")
    try:
        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        if _DEBUG_RAG:
            logger.info("[DEBUG_RAG] RAG API response type=%s keys=%s", type(data).__name__, list(data.keys()) if isinstance(data, dict) else "n/a")
        if isinstance(data, dict):
            chunks = data.get("chunks") or []
            # New endpoint never returns a trace. Keep ``trace`` None
            # so callers that branched on its presence still work.
            trace = None
        elif isinstance(data, list):
            # Defensive: handle list-of-chunks shape if some ancestor
            # of the new endpoint ever returns the bare array.
            chunks = data
            trace = None
        else:
            chunks = []
            trace = None
        if _DEBUG_RAG:
            logger.info("[DEBUG_RAG] chunks len=%s", len(chunks) if chunks else 0)
        out: list[dict[str, Any]] = []
        for idx, c in enumerate(chunks):
            if isinstance(c, dict):
                out.append(dict(c))
            elif isinstance(c, (list, tuple)) and c and all(
                isinstance(x, (list, tuple)) and len(x) == 2 for x in c
            ):
                # Tolerate list-of-pairs shape from older proxies.
                out.append(dict(c))
            else:
                if _DEBUG_RAG:
                    try:
                        t0 = type(c[0]).__name__ if (isinstance(c, (list, tuple)) and c) else "n/a"
                    except (TypeError, IndexError, KeyError):
                        t0 = "n/a"
                    logger.warning("[DEBUG_RAG] RAG API chunk[%s] skip type=%s first_el=%s", idx, type(c).__name__, t0)
                continue
        return out, trace
    except Exception as e:
        logger.warning("RAG API call failed: %s", e)
        return [], None


def retrieve_for_chat(
    question: str,
    top_k: int = 10,
    database_url: str = "",
    filter_payer: str = "",
    filter_state: str = "",
    filter_program: str = "",
    filter_authority_level: str = "",
    n_factual: int | None = None,
    n_hierarchical: int | None = None,
    emitter: Callable[[str], None] | None = None,
    include_trace: bool = False,
    include_document_ids: list[str] | None = None,
    mode: str = "corpus",
    _hybrid_internal: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Call RAG API (if RAG_API_URL set) or inline mobius-retriever.

    Returns (chunks, trace). Chunks have text, document_id, document_name, page_number,
    source_type, match_score, confidence, rerank_score. Trace is None for inline path.

    ``mode`` (Sprint 2 #0.2, 2026-04-24):
        * ``"corpus"``    — hybrid BM25 ⊕ vector via RRF (default).
                            Best for general questions.
        * ``"precision"`` — BM25-only, exact-phrase boost.
                            For code / policy-ID lookups.
        * ``"recall"``    — vector-only, no confidence floor.
                            For exploratory "what do we know about X".

    ``_hybrid_internal`` is set to True when the hybrid path calls
    back into this function for the BM25 arm; it short-circuits the
    mode dispatch to avoid infinite recursion. Callers should not set
    it.
    """
    # ── Mode dispatch (Sprint 2 #0.2) ──────────────────────────────
    # Internal hybrid recursion bypasses dispatch; everything else
    # goes through the mode-aware entry points.
    if not _hybrid_internal:
        m = (mode or "corpus").strip().lower()
        if m == "corpus":
            from app.services.retriever_hybrid import retrieve_corpus_hybrid
            chunks, telemetry = retrieve_corpus_hybrid(
                question=question, top_k=top_k, database_url=database_url,
                filter_payer=filter_payer, filter_state=filter_state,
                filter_program=filter_program, filter_authority_level=filter_authority_level,
                n_factual=n_factual, n_hierarchical=n_hierarchical,
                emitter=emitter, include_document_ids=include_document_ids,
            )
            return chunks, (telemetry if include_trace else None)
        if m == "precision":
            from app.services.retriever_hybrid import retrieve_precision
            chunks, telemetry = retrieve_precision(
                question=question, top_k=top_k, database_url=database_url,
                filter_payer=filter_payer, filter_state=filter_state,
                filter_program=filter_program, filter_authority_level=filter_authority_level,
                n_factual=n_factual, n_hierarchical=n_hierarchical,
                emitter=emitter, include_document_ids=include_document_ids,
            )
            return chunks, (telemetry if include_trace else None)
        if m == "recall":
            from app.services.retriever_hybrid import retrieve_recall
            chunks, telemetry = retrieve_recall(
                question=question, top_k=top_k, emitter=emitter,
            )
            return chunks, (telemetry if include_trace else None)
        # Unknown mode falls through to legacy BM25 path with a warning.
        logger.warning("retrieve_for_chat: unknown mode=%r; using BM25 fallback", mode)
    # _hybrid_internal=True or unknown mode → execute the legacy BM25
    # body below. This is the "precision arm" code path the hybrid
    # wraps around; preserved verbatim so existing callers see no
    # regression.
    def _drop_jpd_emits(base: Callable[[str], None] | None):
        """Filter out JPD tagger and BM25 internal progress before wrap_emitter_for_user."""
        wrapped = wrap_emitter_for_user(base)
        _technical_substrings = (
            "J/P/D tagger", "JPD tagger", "phrase map built", "resolving document_ids",
            "lexicon loaded", "lexicon has 0 phrases", "no tags matched",
            "question matched p=", "BM25 corpus:", "Building BM25",
            "BM25 paragraph matches:", "BM25 sentence matches:", "BM25 returned",
            "Included ", " seed chunk",
        )

        def inner(msg: str) -> None:
            s = (msg or "").strip()
            if not s:
                return
            if any(sub in s for sub in _technical_substrings):
                return
            wrapped(s)
        return inner

    emitter = _drop_jpd_emits(emitter)
    rag_api_url = (os.environ.get("RAG_API_URL") or "").strip()
    rag_path = (os.environ.get("RAG_PATH") or "mobius").strip().lower()
    if rag_path not in ("mobius", "lazy"):
        rag_path = "mobius"

    if rag_api_url:
        chunks, trace = retrieve_via_rag_api(
            question,
            path=rag_path,
            top_k=top_k,
            apply_google=False,  # React tool loop invokes Google explicitly when needed
            n_factual=n_factual,
            n_hierarchical=n_hierarchical,
            emitter=emitter,
            include_trace=include_trace,
            filter_payer=filter_payer,
            filter_state=filter_state,
            filter_program=filter_program,
            filter_authority_level=filter_authority_level,
        )
        if chunks:
            _debug_chunks("rag_api return", chunks)
            return chunks, trace
        # RAG API failed or returned empty; fall back to inline BM25 if database_url available
        if database_url:
            logger.info("RAG API returned no chunks; falling back to inline BM25")

    # Inline BM25 (primary when RAG_API_URL unset, or fallback when API fails)
    try:
        from mobius_retriever.retriever import retrieve_bm25
        from mobius_retriever.config import apply_normalize_bm25, load_bm25_sigmoid_config, load_reranker_config
        from mobius_retriever.reranker import rerank_with_config
        from mobius_retriever.jpd_tagger import (
            tag_question_and_resolve_document_ids,
            fetch_document_tags_by_ids,
            fetch_line_tags_for_chunks,
        )
    except ImportError as e:
        logger.warning("mobius-retriever not installed: %s", e)
        return [], None

    if not database_url:
        _emit(emitter, "RAG database URL not set; skipping retrieval.")
        return [], None

    tag_filters: dict[str, str] = {}
    if filter_payer:
        tag_filters["document_payer"] = filter_payer
    if filter_state:
        tag_filters["document_state"] = filter_state
    if filter_program:
        tag_filters["document_program"] = filter_program
    if filter_authority_level:
        tag_filters["document_authority_level"] = filter_authority_level

    # Inline path: no trace (run_rag_pipeline is only used by RAG API)
    result = retrieve_bm25(
        question=question,
        postgres_url=database_url,
        rag_database_url=database_url,
        authority_level=filter_authority_level or None,
        tag_filters=tag_filters or None,
        top_k=top_k,
        use_jpd_tagger=True,
        emitter=emitter,
        include_document_ids=include_document_ids,
    )

    bm25_cfg = load_bm25_sigmoid_config()
    chunks_to_convert = result.raw

    # Rerank: retrieve → rerank → assemble
    try:
        reranker_cfg = load_reranker_config(_DEFAULT_RERANKER_CONFIG)
        if reranker_cfg.signals and chunks_to_convert:
            dicts = []
            for c in chunks_to_convert:
                if not isinstance(c, dict):
                    continue
                try:
                    dicts.append(_bm25_to_rerank_dict(c, bm25_cfg))
                except (TypeError, AttributeError, KeyError) as e:
                    logger.debug("Skip chunk (not dict-like): %s", e)
                    continue
            doc_ids = list({str(d.get("document_id", "")) for d in dicts if d.get("document_id")})
            doc_tags_by_id = fetch_document_tags_by_ids(database_url, doc_ids) if doc_ids else {}
            line_tags_by_key = fetch_line_tags_for_chunks(database_url, dicts) if dicts else {}
            # Use emitter=None — this call is only for reranker qtags; JPD progress emits are internal
            jpd = tag_question_and_resolve_document_ids(question, database_url, emitter=None)
            qtags = jpd if ("tag_match" in (reranker_cfg.signals or {}) and jpd.has_tags) else None
            chunks_to_convert = rerank_with_config(
                dicts,
                reranker_cfg,
                question_tags=qtags,
                doc_tags_by_id=doc_tags_by_id,
                line_tags_by_key=line_tags_by_key,
            )
            _debug_chunks("after rerank (chunks_to_convert)", chunks_to_convert)
    except FileNotFoundError as _fnf:
        # 2026-04-21: was logger.debug, which silently masked a
        # real prod bug — configs/*.yaml didn't ship in the
        # mobius-retriever wheel, so rerank fell back to BM25-only
        # scoring in Cloud Run while working fine in dev editable
        # installs. Upgraded to warning so this is never silent again.
        logger.warning(
            "Reranker config not found (%s); falling back to BM25-only "
            "scoring. tag_match contribution will be zero.", _fnf,
        )
    except Exception as e:
        logger.warning("Reranker failed: %s; using BM25 scores only.", e, exc_info=True)

    def _to_plain_dict(c: Any) -> dict[str, Any]:
        """Ensure chunk is a plain dict; handle Row/dict subclasses and list-of-pairs."""
        if isinstance(c, dict):
            return dict(c)
        if isinstance(c, (list, tuple)) and c and all(
            isinstance(x, (list, tuple)) and len(x) == 2 for x in c
        ):
            return dict(c)
        raise TypeError(f"Chunk must be dict or list of (k,v) pairs, got {type(c).__name__}")

    out: list[dict[str, Any]] = []
    for i, c in enumerate(chunks_to_convert):
        if not isinstance(c, dict):
            logger.warning("[DEBUG_RAG] inline chunk[%s] NOT dict type=%s skipping", i, type(c).__name__)
            continue
        c = _to_plain_dict(c)
        raw = c.get("raw_score")
        pt = c.get("provision_type", "sentence")
        if raw is not None and bm25_cfg:
            match_score = apply_normalize_bm25(float(raw), pt, bm25_cfg)
        elif raw is not None:
            match_score = min(1.0, float(raw) / 50.0)
        else:
            match_score = c.get("similarity") or c.get("rerank_score")
        out.append(_raw_to_chat_chunk(c, match_score))

    # Apply blend selection: without this, BM25 sentence fragments always outscore
    # paragraphs because of higher per-word keyword density. Blend selection ensures
    # n_hierarchical paragraph slots are filled before n_factual sentence slots.
    if (n_factual is not None or n_hierarchical is not None) and out:
        try:
            from mobius_retriever.assemble import _apply_blend_selection
            out = _apply_blend_selection(out, n_factual, n_hierarchical)
            if _DEBUG_RAG:
                para_n = sum(1 for c in out if (c.get("provision_type") or "") == "paragraph")
                sent_n = sum(1 for c in out if (c.get("provision_type") or "") == "sentence")
                logger.info(
                    "[DEBUG_RAG retriever] blend applied n_hier=%s n_fact=%s → para=%s sent=%s total=%s",
                    n_hierarchical, n_factual, para_n, sent_n, len(out),
                )
        except Exception as e:
            logger.debug("Blend selection failed (non-fatal): %s", e)

    _debug_chunks("inline return (out)", out)
    return out, None
