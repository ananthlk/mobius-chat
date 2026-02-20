"""Persist retrieval runs and docs for data science. Uses CHAT_RAG_DATABASE_URL."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def _get_db_url() -> str:
    from app.chat_config import get_chat_config
    return (get_chat_config().rag.database_url or "").strip()


def insert_retrieval_run(
    correlation_id: str,
    subquestion_id: str | None,
    subquestion_text: str | None,
    path: str,
    n_factual: int | None,
    n_hierarchical: int | None,
    trace: dict[str, Any],
    assembled: list[dict[str, Any]],
) -> None:
    """Insert retrieval_runs and retrieval_docs from trace and assembled chunks."""
    url = _get_db_url()
    if not url:
        logger.debug("CHAT_RAG_DATABASE_URL not set; retrieval run not persisted")
        return
    if not trace or not isinstance(trace, dict) or not trace.get("extract"):
        return

    ex = trace.get("extract") or {}
    mrg = trace.get("merge") or {}
    rr = trace.get("rerank") or {}
    if not isinstance(ex, dict):
        ex = {}
    if not isinstance(mrg, dict):
        mrg = {}
    if not isinstance(rr, dict):
        rr = {}
    dc = trace.get("decay_per_category") or []
    bl = trace.get("blend_selection") or {}

    run_id = uuid.uuid4()
    path_val = path or (trace.get("path") if isinstance(trace.get("path"), str) else None) or "mobius"
    run_row = {
        "id": run_id,
        "correlation_id": correlation_id,
        "subquestion_id": subquestion_id,
        "subquestion_text": (subquestion_text or "")[:2000] if subquestion_text else None,
        "path": path_val,
        "n_factual": n_factual,
        "n_hierarchical": n_hierarchical,
        "bm25_raw_n": ex.get("bm25_raw_n"),
        "vector_raw_n": ex.get("vector_raw_n"),
        "vector_filtered_n": ex.get("vector_filtered_n"),
        "merged_n": ex.get("merged_n"),
        "n_added_bm25": mrg.get("n_added_bm25"),
        "n_skipped_bm25": mrg.get("n_skipped_bm25"),
        "n_added_vector": mrg.get("n_added_vector"),
        "n_skipped_vector": mrg.get("n_skipped_vector"),
        "merged_ids_by_source": json.dumps(mrg.get("merged_ids_by_source")) if mrg.get("merged_ids_by_source") else None,
        "n_chunks_rerank_input": rr.get("n_chunks_input"),
        "n_chunks_after_decay": rr.get("n_chunks_after_decay"),
        "by_category_keys": json.dumps(rr.get("by_category_keys")) if rr.get("by_category_keys") else None,
        "decay_per_category": json.dumps(dc) if dc else None,
        "blend_chunks_input_n": bl.get("chunks_input_n"),
        "blend_n_sentence_pool": bl.get("n_sentence_level_pool"),
        "blend_n_paragraph_pool": bl.get("n_paragraph_level_pool"),
        "blend_n_output": bl.get("n_output"),
        "n_assembled": trace.get("n_assembled"),
        "n_corpus": trace.get("n_corpus"),
        "n_google": trace.get("n_google"),
        "reranker_config_snapshot": json.dumps(rr.get("reranker_config_snapshot")) if rr.get("reranker_config_snapshot") else None,
        "bm25_sigmoid_snapshot": json.dumps(ex.get("bm25_sigmoid_snapshot")) if ex.get("bm25_sigmoid_snapshot") else None,
        "raw_by_signal": json.dumps(rr.get("raw_by_signal")) if rr.get("raw_by_signal") else None,
        "norm_by_signal": json.dumps(rr.get("norm_by_signal")) if rr.get("norm_by_signal") else None,
        "extract_ms": ex.get("extract_ms"),
        "merge_ms": ex.get("merge_ms"),
        "rerank_ms": rr.get("rerank_ms"),
        "assemble_ms": trace.get("assemble_ms"),
    }

    per_chunk_by_id: dict[str, dict[str, Any]] = {}
    for pc in rr.get("per_chunk") or []:
        if not isinstance(pc, dict):
            continue
        cid = pc.get("id", "")
        if cid:
            per_chunk_by_id[cid] = pc

    bm25_sigmoid = ex.get("bm25_sigmoid_snapshot") or {}
    if not isinstance(bm25_sigmoid, dict):
        bm25_sigmoid = {}

    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO retrieval_runs (
                id, correlation_id, subquestion_id, subquestion_text, path,
                n_factual, n_hierarchical, bm25_raw_n, vector_raw_n, vector_filtered_n,
                merged_n, n_added_bm25, n_skipped_bm25, n_added_vector, n_skipped_vector,
                merged_ids_by_source, n_chunks_rerank_input, n_chunks_after_decay,
                by_category_keys, decay_per_category, blend_chunks_input_n,
                blend_n_sentence_pool, blend_n_paragraph_pool, blend_n_output,
                n_assembled, n_corpus, n_google,
                reranker_config_snapshot, bm25_sigmoid_snapshot, raw_by_signal, norm_by_signal,
                extract_ms, merge_ms, rerank_ms, assemble_ms
            )
            VALUES (
                %(id)s, %(correlation_id)s, %(subquestion_id)s, %(subquestion_text)s, %(path)s,
                %(n_factual)s, %(n_hierarchical)s, %(bm25_raw_n)s, %(vector_raw_n)s, %(vector_filtered_n)s,
                %(merged_n)s, %(n_added_bm25)s, %(n_skipped_bm25)s, %(n_added_vector)s, %(n_skipped_vector)s,
        %(merged_ids_by_source)s, %(n_chunks_rerank_input)s, %(n_chunks_after_decay)s,
        %(by_category_keys)s, %(decay_per_category)s, %(blend_chunks_input_n)s,
        %(blend_n_sentence_pool)s, %(blend_n_paragraph_pool)s, %(blend_n_output)s,
        %(n_assembled)s, %(n_corpus)s, %(n_google)s,
        %(reranker_config_snapshot)s, %(bm25_sigmoid_snapshot)s,
        %(raw_by_signal)s, %(norm_by_signal)s,
                %(extract_ms)s, %(merge_ms)s, %(rerank_ms)s, %(assemble_ms)s
            )
            """,
            run_row,
        )

        for idx, c in enumerate(assembled, 1):
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id", ""))
            src = c.get("retrieval_source", "vector")
            pt = c.get("provision_type", "paragraph")
            pc = per_chunk_by_id.get(cid, {}) if cid else {}
            raw = c.get("raw_score") or pc.get("raw_score")
            pt_cfg = bm25_sigmoid.get(pt, {}) if isinstance(bm25_sigmoid, dict) else {}
            sig_k = pt_cfg.get("k") if isinstance(pt_cfg, dict) else None
            sig_x0 = pt_cfg.get("x0") if isinstance(pt_cfg, dict) else None

            reranker_signals = pc.get("signals")

            cur.execute(
                """
                INSERT INTO retrieval_docs (
                    retrieval_run_id, chunk_index, chunk_id, document_id, document_name,
                    page_number, retrieval_source, provision_type,
                    bm25_raw_score, bm25_sigmoid_k, bm25_sigmoid_x0, similarity, rerank_score,
                    reranker_signals, confidence_label, text_preview
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    run_id,
                    idx,
                    cid[:256] if cid else None,
                    str(c.get("document_id", ""))[:256] if c.get("document_id") else None,
                    (c.get("document_name") or "")[:512],
                    c.get("page_number"),
                    src[:64] if src else None,
                    pt[:32] if pt else None,
                    float(raw) if raw is not None else None,
                    float(sig_k) if sig_k is not None else None,
                    float(sig_x0) if sig_x0 is not None else None,
                    float(c.get("similarity")) if c.get("similarity") is not None else float(c.get("rerank_score")) if c.get("rerank_score") is not None else None,
                    float(c.get("rerank_score")) if c.get("rerank_score") is not None else None,
                    json.dumps(reranker_signals) if reranker_signals else None,
                    (c.get("confidence_label") or "")[:64],
                    (c.get("text") or "")[:500],
                ),
            )

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Failed to persist retrieval run: %s", e)
