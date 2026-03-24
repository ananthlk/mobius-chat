#!/usr/bin/env python3
"""Run a question through BM25 + neighbor expansion only (no vector, no LLM). Shows retrieved chunks and neighbors.

Usage (from Mobius root, with CHAT_RAG_DATABASE_URL set):
  set -a && . mobius-chat/.env; set +a
  PYTHONPATH=mobius-chat:mobius-retriever/src python mobius-chat/scripts/trace_question_bm25_neighbors.py "What are a member's rights regarding choice of behavioral health provider under Sunshine Health?"
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))
_root = CHAT_ROOT.parent
for env_path in (CHAT_ROOT / ".env", _root / "mobius-config" / ".env", _root / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        break


def _truncate(t: str, n: int = 85) -> str:
    t = (t or "").strip().replace("\n", " ")
    return (t[: n - 3] + "...") if len(t) > n else t


def main() -> int:
    question = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else None
    if not question:
        print("Usage: python trace_question_bm25_neighbors.py \"Your question here\"")
        return 1

    db_url = (os.environ.get("CHAT_RAG_DATABASE_URL") or "").strip()
    if not db_url:
        print("CHAT_RAG_DATABASE_URL not set. Set it or source mobius-chat/.env")
        return 1

    print("=" * 80)
    print("BM25 + NEIGHBOR EXPANSION (no vector, no LLM)")
    print("=" * 80)
    print(f"\nQuestion: {question}\n")

    # BM25 only
    from mobius_retriever.retriever import retrieve_bm25
    from mobius_retriever.config import load_path_b_config, load_bm25_sigmoid_config, apply_normalize_bm25
    from mobius_retriever.assemble import assemble_docs

    cfg_path = _root / "mobius-retriever" / "configs" / "path_b_v1.yaml"
    if not cfg_path.exists():
        cfg_path = _root / "configs" / "path_b_v1.yaml"
    config = load_path_b_config(cfg_path) if cfg_path.exists() else None
    if config:
        config.postgres_url = db_url
        config.rag_database_url = db_url or ""

    result = retrieve_bm25(
        question=question,
        postgres_url=db_url,
        rag_database_url=db_url,
        config=config,
        top_k=15,
        use_jpd_tagger=True,
        emitter=lambda m: print(f"  [BM25] {m}"),
    )
    raw = result.raw
    print(f"\nBM25 raw chunks: {len(raw)}")

    bm25_cfg = load_bm25_sigmoid_config()
    chunks_for_assembly = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        raw_score = c.get("raw_score")
        pt = c.get("provision_type", "sentence")
        match = apply_normalize_bm25(float(raw_score), pt, bm25_cfg) if raw_score is not None else 0.0
        chunks_for_assembly.append({
            "id": c.get("id"),
            "text": c.get("text") or "",
            "document_id": c.get("document_id"),
            "document_name": c.get("document_name") or "document",
            "page_number": c.get("page_number"),
            "paragraph_index": c.get("paragraph_index"),
            "source_type": c.get("source_type", "chunk"),
            "match_score": match,
            "confidence": match,
            "rerank_score": match,
            "retrieval_source": "bm25_paragraph" if pt == "paragraph" else "bm25_sentence",
            "provision_type": pt,
        })

    # Assemble with neighbor expansion
    assembled = assemble_docs(
        chunks_for_assembly,
        question,
        apply_google=False,
        expand_neighbors=True,
        database_url=db_url,
        neighbor_window=2,
    )

    n_core = sum(1 for c in assembled if not c.get("is_neighbor"))
    n_neighbor = sum(1 for c in assembled if c.get("is_neighbor"))
    print(f"\nAfter neighbor expansion: {len(assembled)} total ({n_core} core + {n_neighbor} neighbors)")

    print("\n" + "=" * 80)
    print("ASSEMBLED DOCS (core + neighbors)")
    print("=" * 80)
    for i, c in enumerate(assembled[:35], 1):
        doc_name = (c.get("document_name") or "doc")[:32]
        page = c.get("page_number")
        para_idx = c.get("paragraph_index")
        is_neighbor = c.get("is_neighbor", False)
        tag = " [NEIGHBOR]" if is_neighbor else ""
        rs = c.get("rerank_score") or c.get("match_score")
        rs_str = f"{rs:.3f}" if rs is not None else "?"
        pt = c.get("provision_type", "?")
        page_para = f"p{page}" if page is not None else "?"
        if para_idx is not None:
            page_para += f" para#{para_idx}"
        print(f"\n  {i}. {doc_name:32} {page_para:14} {pt:10} score={rs_str}{tag}")
        print(f"      {_truncate(c.get('text') or '', 88)}")

    print("\n" + "=" * 80)
    print("To get the full answer with LLM, run (requires Vertex billing):")
    print("  PYTHONPATH=mobius-chat:mobius-retriever/src python mobius-chat/scripts/trace_dev_002_mobius.py --inline -q \"...\"")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
