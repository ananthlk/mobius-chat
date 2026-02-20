#!/usr/bin/env python3
"""Trace doc assembly for question 2 (Express Scripts phone) - inject known corpus chunk, follow assembler.

The dev_retrieval_report shows the perfect answer: "Pharmacies may call the Express Scripts help desk at 1-833-750-4392"
with rerank_score 0.677 from mobius-retriever's BM25+reranker.

Chat's published_rag uses Vertex (match_score = 1 - distance/2). This script injects chunks
that simulate what retrieval would return, then traces assign_confidence and apply_google_fallback.

Run: PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_assembler_q2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

from app.services.doc_assembly import (
    assign_confidence,
    assign_confidence_batch,
    filter_abstain,
    best_score,
    apply_google_fallback,
    assemble_docs,
    DocAssemblyConfig,
)

QUESTION = "What phone number do pharmacies call for Express Scripts help desk?"
PERFECT_CHUNK = {
    "id": "3b543bcb-8074-4934-a277-15bb4d366a12",
    "text": "Pharmacies may call the Express Scripts help desk at 1-833-750-4392.",
    "document_id": "doc-sunshine-provider",
    "document_name": "Sunshine Provider Manual",
    "page_number": 1,
    "source_type": "sentence",
}


def main() -> None:
    cfg = DocAssemblyConfig()
    print("DocAssemblyConfig thresholds:")
    print(f"  confidence_abstain_max: {cfg.confidence_abstain_max}  (< this = abstain)")
    print(f"  confidence_process_confident_min: {cfg.confidence_process_confident_min}  (>= this = process_confident)")
    print(f"  [0.5, 0.85) = process_with_caution")
    print()

    # Case 1: Chunk with match_score from Vertex (what published_rag would return)
    # Vertex returns distance; match_score = 1 - distance/2. Good match: distance ~0.2 -> match_score 0.9
    print("=" * 60)
    print("CASE 1: Corpus chunk with match_score=0.9 (Vertex good match)")
    print("=" * 60)
    c1 = dict(PERFECT_CHUNK, match_score=0.9, confidence=0.9)
    out1 = assign_confidence(c1, cfg)
    print(f"  Input:  match_score=0.9, confidence=0.9")
    print(f"  Output: rerank_score={out1['rerank_score']}, confidence_label={out1['confidence_label']}")
    print(f"  → {out1['llm_guidance']}")
    print()

    # Case 2: Chunk with rerank_score 0.677 (from mobius-retriever BM25+reranker - what dev_retrieval_report shows)
    print("=" * 60)
    print("CASE 2: Corpus chunk with rerank_score=0.677 (mobius-retriever style)")
    print("=" * 60)
    c2 = dict(PERFECT_CHUNK, rerank_score=0.677)
    out2 = assign_confidence(c2, cfg)
    print(f"  Input:  rerank_score=0.677")
    print(f"  Output: rerank_score={out2['rerank_score']}, confidence_label={out2['confidence_label']}")
    print(f"  → 0.677 is in [0.5, 0.85) → process_with_caution")
    print()

    # Case 3: Chunk with low score
    print("=" * 60)
    print("CASE 3: Corpus chunk with match_score=0.3 (weak match)")
    print("=" * 60)
    c3 = dict(PERFECT_CHUNK, match_score=0.3)
    out3 = assign_confidence(c3, cfg)
    print(f"  Input:  match_score=0.3")
    print(f"  Output: rerank_score={out3['rerank_score']}, confidence_label={out3['confidence_label']}")
    print()

    # Case 4: Full assemble_docs with corpus chunks (no Google)
    print("=" * 60)
    print("CASE 4: assemble_docs([perfect_chunk], apply_google=True)")
    print("        Simulating retrieval returned 1 corpus chunk with match_score=0.9")
    print("=" * 60)
    chunks_in = [dict(PERFECT_CHUNK, match_score=0.9, confidence=0.9)]
    emitted = []
    out4 = assemble_docs(
        chunks_in,
        QUESTION,
        apply_google=True,
        expand_neighbors=False,
        emitter=emitted.append,
    )
    print(f"  Chunks in: {len(chunks_in)}")
    print(f"  Chunks out: {len(out4)}")
    print(f"  Assembly messages: {emitted}")
    for i, c in enumerate(out4[:3], 1):
        print(f"  [{i}] confidence_label={c.get('confidence_label')} rerank_score={c.get('rerank_score')} | {c.get('text', '')[:60]}...")
    print()

    # Case 5: Why report showed abstain - retrieval returned 0, so only Google results
    print("=" * 60)
    print("CASE 5: Why dev_chat_pipeline_report showed 'abstain' for all")
    print("=" * 60)
    print("  Retrieval (published_rag_search) returned 0 chunks (Vertex 403 / no results).")
    print("  So assemble_docs was called with [] or got 0 from retrieval.")
    print("  apply_google_fallback: best=0 → 'Low corpus confidence' → Google only.")
    print("  Google results are ALWAYS abstain (external source, not from corpus).")
    print()
    print("  If retrieval HAD returned the corpus chunk with match_score=0.9:")
    print("    → assign_confidence would give process_confident (>= 0.85)")
    print("    → No Google fallback (best >= 0.85)")
    print("    → Chunk sent to LLM with process_confident")
    print()
    print("  If retrieval returned chunk with match_score=0.67 (Vertex distance ~0.66):")
    print("    → assign_confidence would give process_with_caution (0.5-0.85)")
    print("    → Google complement added (best in [0.5, 0.85))")
    print("    → Corpus chunk + Google results, both sent")


if __name__ == "__main__":
    main()
