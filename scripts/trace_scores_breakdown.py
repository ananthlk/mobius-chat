#!/usr/bin/env python3
"""Trace score breakdown: raw BM25 → normalized (sigmoid) → cutoff → reranker.

Shows whether chunks are dropped at the BM25 cutoff (data vs logic) or at reranker.
Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_scores_breakdown.py "How to file a grievance"
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


def _n_tags_from_jpd(jpd) -> int:
    return len(jpd.p_tags or {}) + len(jpd.d_tags or {}) + len(jpd.j_tags or {})


def main() -> int:
    question = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else "How to file a grievance"
    db_url = (os.environ.get("CHAT_RAG_DATABASE_URL") or "").strip()
    if not db_url:
        print("CHAT_RAG_DATABASE_URL not set.")
        return 1

    from mobius_retriever.retriever import retrieve_bm25
    from mobius_retriever.config import (
        apply_normalize_bm25,
        load_bm25_sigmoid_config,
        load_retrieval_cutoffs,
    )

    # 1. Get RAW chunks from bm25_search (before cutoff) - we need to call bm25_search directly
    #    because retrieve_bm25 applies the cutoff. Use bm25_search + manual cutoff simulation.
    from mobius_retriever.bm25_search import bm25_search

    emitted: list[str] = []

    def emit(m: str) -> None:
        if m.strip():
            emitted.append(m.strip())

    from mobius_retriever.jpd_tagger import tag_question_and_resolve_document_ids
    jpd = tag_question_and_resolve_document_ids(question, db_url, emitter=emit)
    document_ids = jpd.document_ids if jpd.has_document_ids else None
    n_tags = _n_tags_from_jpd(jpd)

    raw_chunks = bm25_search(
        question=question,
        postgres_url=db_url,
        document_ids=document_ids,
        top_k=10,
        include_paragraphs=True,
        top_k_per_type=10,
        emitter=emit,
    )

    bm25_cfg = load_bm25_sigmoid_config()
    cutoffs = load_retrieval_cutoffs()
    cutoff_norm = cutoffs.bm25_abstention_cutoff_normalized
    cutoff_raw = cutoffs.bm25_abstention_cutoff_raw

    # Sigmoid params
    pt_cfg = (bm25_cfg or {}).get("provision_types") or {}
    print("=" * 80)
    print("SCORE BREAKDOWN: raw → normalized → cutoff")
    print("=" * 80)
    print(f"\nQuestion: {question}")
    p_codes = list((jpd.p_tags or {}).keys())
    d_codes = list((jpd.d_tags or {}).keys())
    j_codes = list((jpd.j_tags or {}).keys())
    print(f"\nJPD tagger: p={p_codes} d={d_codes} j={j_codes}")
    print(f"  document_ids: {len(document_ids or [])} docs")
    print(f"\nSigmoid config (bm25_sigmoid.yaml):")
    for pt, cfg in pt_cfg.items():
        print(f"  {pt}: k={cfg.get('k')} x0={cfg.get('x0')}")
    print(f"\nCutoff: normalized >= {cutoff_norm} (raw equivalent ≈ {cutoff_raw})")
    print(f"  Formula: norm = sigmoid(k * (raw - x0))")

    print("\n" + "-" * 80)
    print("BM25 OUTPUT (before cutoff filter)")
    print("-" * 80)
    print(f"Total chunks from BM25: {len(raw_chunks)}")
    if not raw_chunks:
        print("No chunks — corpus may be empty or query has no matches.")
        return 0

    # Score-per-tag: adjusted_raw = raw / max(1, n_tags)
    divisor = max(1, n_tags)
    print(f"\nScore-per-tag divisor: max(1, n_tags) = {divisor}")

    rows: list[dict] = []
    for c in raw_chunks:
        raw = c.get("raw_score")
        adjusted = (float(raw) / divisor) if raw is not None else None
        pt = c.get("provision_type", "sentence")
        if adjusted is None:
            norm = None
            passed = False
        elif bm25_cfg:
            norm = apply_normalize_bm25(adjusted, pt, bm25_cfg)
            passed = norm >= cutoff_norm
        else:
            norm = min(1.0, adjusted / 50.0)
            passed = norm >= cutoff_norm
        rows.append({
            "id": str(c.get("id", ""))[:24],
            "pt": pt,
            "raw": raw,
            "adjusted": adjusted,
            "norm": norm,
            "passed": passed,
            "snippet": (c.get("text") or "")[:60].replace("\n", " "),
        })

    # Sort by raw desc
    rows.sort(key=lambda r: (r["raw"] or 0), reverse=True)

    print(f"\n{'#':>3} {'provision':>10} {'raw':>10} {'adjusted':>10} {'normalized':>10} {'cutoff':>8} {'pass':>6}  snippet")
    print("-" * 100)
    n_passed = 0
    for i, r in enumerate(rows, 1):
        norm_s = f"{r['norm']:.4f}" if r["norm"] is not None else "—"
        pass_s = "YES" if r["passed"] else "NO"
        if r["passed"]:
            n_passed += 1
        adj_s = f"{r['adjusted']:.4f}" if r.get("adjusted") is not None else "—"
        print(f"{i:3} {r['pt']:>10} {r['raw']:>10.4f} {adj_s:>10} {norm_s:>10} {cutoff_norm:>8.2f} {pass_s:>6}  {r['snippet'][:45]}...")

    print(f"\nChunks passing cutoff: {n_passed} / {len(rows)}")

    if n_passed == 0:
        print("\n>>> All chunks dropped by BM25 abstention cutoff.")
        print("    Possible causes: sigmoid x0 too high (raw scores below x0 → norm < 0.5), or cutoff too strict.")

    # 2. If we had passed chunks, run reranker and show reranker breakdown
    if n_passed > 0:
        print("\n" + "-" * 80)
        print("RERANKER (would run on passed chunks)")
        print("-" * 80)
        try:
            from mobius_retriever.config import load_reranker_config
            from mobius_retriever.reranker import rerank_with_config
            from mobius_retriever.jpd_tagger import fetch_document_tags_by_ids, fetch_line_tags_for_chunks

            def _norm(c):
                raw = c.get("raw_score")
                adj = (float(raw) / divisor) if raw is not None else 0.0
                pt = c.get("provision_type", "sentence")
                if raw is None:
                    return 0.0
                if bm25_cfg:
                    return apply_normalize_bm25(adj, pt, bm25_cfg)
                return min(1.0, adj / 50.0)

            passed_chunks = [c for c in raw_chunks if _norm(c) >= cutoff_norm]
            # Convert to reranker input format
            dicts = []
            for c in passed_chunks:
                raw = c.get("raw_score")
                adj = (float(raw) / divisor) if raw is not None else 0.0
                pt = c.get("provision_type", "sentence")
                sim = apply_normalize_bm25(adj, pt, bm25_cfg) if raw is not None and bm25_cfg else 0.0
                dicts.append({
                    "id": c.get("id"),
                    "text": c.get("text", ""),
                    "document_id": c.get("document_id"),
                    "document_name": c.get("document_name", "document"),
                    "similarity": sim,
                    "raw_score": raw,
                    "provision_type": pt,
                    "retrieval_source": f"bm25_{pt}",
                })
            doc_ids = list({str(d.get("document_id", "")) for d in dicts if d.get("document_id")})
            doc_tags = fetch_document_tags_by_ids(db_url, doc_ids) if doc_ids else {}
            line_tags = fetch_line_tags_for_chunks(db_url, dicts) if dicts else {}
            reranker_cfg = load_reranker_config("configs/reranker_v1.yaml")
            qtags = jpd if ("tag_match" in (reranker_cfg.signals or {})) else None
            trace: dict = {}
            reranked = rerank_with_config(
                dicts,
                reranker_cfg,
                question_tags=qtags,
                doc_tags_by_id=doc_tags,
                line_tags_by_key=line_tags,
                trace=trace,
            )
            per_chunk = trace.get("rerank", {}).get("per_chunk") or []
            if per_chunk:
                print("\nPer-chunk reranker signals (tag_match, authority_level, etc.):")
                for i, pc in enumerate(per_chunk[:10], 1):
                    sigs = pc.get("signals") or {}
                    rr = pc.get("rerank_score")
                    tag = sigs.get("tag_match", {})
                    print(f"  {i}. rerank_score={rr:.4f} tag_match={tag}")
        except Exception as e:
            print(f"Reranker trace failed: {e}")
    else:
        print("\n(Skipping reranker — no chunks passed cutoff.)")

    print("\n" + "=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
