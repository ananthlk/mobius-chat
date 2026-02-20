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
    return {
        "id": c.get("id"),
        "text": c.get("text") or "",
        "document_id": c.get("document_id"),
        "document_name": c.get("document_name") or "document",
        "page_number": c.get("page_number"),
        "source_type": c.get("source_type") or "chunk",
        "document_authority_level": c.get("document_authority_level"),
        "match_score": match_score,
        "confidence": match_score,
        "rerank_score": c.get("rerank_score") or match_score,
        "raw_score": c.get("raw_score"),
        "provision_type": c.get("provision_type", "sentence"),
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
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Call RAG API. Returns (assembled docs, trace or None). Docs already have confidence labels."""
    url = (os.environ.get("RAG_API_URL") or "").strip()
    if not url:
        return [], None
    base = url.rstrip("/")
    api_url = f"{base}/retrieve"
    payload_obj: dict = {
        "question": question,
        "path": path if path in ("mobius", "lazy") else "mobius",
        "top_k": top_k,
        "apply_google": apply_google,
        "include_trace": include_trace,
    }
    if n_factual is not None:
        payload_obj["n_factual"] = n_factual
    if n_hierarchical is not None:
        payload_obj["n_hierarchical"] = n_hierarchical
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
        if not isinstance(data, dict):
            # Handle legacy / proxy response that returns a list of docs at top level
            docs = data if isinstance(data, list) else []
            trace = None
        else:
            docs = data.get("docs") or []
            trace = data.get("retrieval_trace") if include_trace else None
        # Normalize to plain dicts (API may return Row-like or list-of-pairs)
        out: list[dict[str, Any]] = []
        for d in docs:
            if isinstance(d, dict):
                out.append(dict(d))
            elif isinstance(d, (list, tuple)) and d and all(
                isinstance(x, (list, tuple)) and len(x) == 2 for x in d
            ):
                out.append(dict(d))
            else:
                raise TypeError(
                    f"RAG API doc must be dict or list of (k,v) pairs, got {type(d).__name__}"
                )
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
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Call RAG API (if RAG_API_URL set) or inline mobius-retriever.

    Returns (chunks, trace). Chunks have text, document_id, document_name, page_number,
    source_type, match_score, confidence, rerank_score. Trace is None for inline path.
    """
    emitter = wrap_emitter_for_user(emitter)
    rag_api_url = (os.environ.get("RAG_API_URL") or "").strip()
    rag_path = (os.environ.get("RAG_PATH") or "mobius").strip().lower()
    if rag_path not in ("mobius", "lazy"):
        rag_path = "mobius"

    if rag_api_url:
        _emit(emitter, "Searching our materials...")
        chunks, trace = retrieve_via_rag_api(
            question,
            path=rag_path,
            top_k=top_k,
            apply_google=True,
            n_factual=n_factual,
            n_hierarchical=n_hierarchical,
            emitter=emitter,
            include_trace=include_trace,
        )
        if chunks:
            _emit(emitter, f"Using {len(chunks)} result{'s' if len(chunks) != 1 else ''} to answer this part.")
        return chunks, trace

    # Fallback: inline BM25
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

    _emit(emitter, "Searching our materials...")
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
    )

    bm25_cfg = load_bm25_sigmoid_config()
    chunks_to_convert = result.raw

    # Rerank: retrieve → rerank → assemble
    try:
        reranker_cfg = load_reranker_config(_DEFAULT_RERANKER_CONFIG)
        if reranker_cfg.signals and chunks_to_convert:
            dicts = [_bm25_to_rerank_dict(c, bm25_cfg) for c in chunks_to_convert]
            doc_ids = list({str(d.get("document_id", "")) for d in dicts if d.get("document_id")})
            doc_tags_by_id = fetch_document_tags_by_ids(database_url, doc_ids) if doc_ids else {}
            line_tags_by_key = fetch_line_tags_for_chunks(database_url, dicts) if dicts else {}
            jpd = tag_question_and_resolve_document_ids(question, database_url, emitter=emitter)
            qtags = jpd if ("tag_match" in (reranker_cfg.signals or {}) and jpd.has_tags) else None
            chunks_to_convert = rerank_with_config(
                dicts,
                reranker_cfg,
                question_tags=qtags,
                doc_tags_by_id=doc_tags_by_id,
                line_tags_by_key=line_tags_by_key,
            )
    except FileNotFoundError:
        logger.debug("Reranker config not found; using BM25 scores only.")
    except Exception as e:
        logger.warning("Reranker failed: %s; using BM25 scores only.", e)

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
    for c in chunks_to_convert:
        if not isinstance(c, dict):
            logger.debug("Skipping non-dict chunk: %s", type(c).__name__)
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

    if out:
        _emit(emitter, f"Using {len(out)} result{'s' if len(out) != 1 else ''} to answer this part.")
    return out, None
