#!/usr/bin/env python3
"""Trace retrieval stages: where do chunks get dropped?

Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_retrieval_stages.py "how do I file an appeal for Sunshine Health"

Prints: retrieval -> rerank -> assemble (blend) -> confidence/filter_abstain -> final sent to LLM.
Uses RAG API with include_trace to get pipeline stage counts.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CHAT_ROOT = Path(__file__).resolve().parent.parent
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


def main() -> int:
    question = " ".join(sys.argv[1:]).strip()
    if not question:
        print("Usage: python trace_retrieval_stages.py <question>")
        print('Example: python trace_retrieval_stages.py "how do I file an appeal for Sunshine Health"')
        return 1

    payer = "Sunshine Health"
    rag_api_url = (os.environ.get("RAG_API_URL") or "").strip()
    if not rag_api_url:
        print("RAG_API_URL not set; cannot trace RAG API pipeline.")
        return 1

    from app.services.retrieval_calibration import get_retrieval_blend, intent_to_score
    score = intent_to_score("canonical")  # appeal question = canonical
    params = get_retrieval_blend(score)
    n_h = params.get("n_hierarchical", 0)
    n_f = params.get("n_factual", 0)
    confidence_min = params.get("confidence_min", 0.5)

    print("=" * 70)
    print("RETRIEVAL STAGE TRACE")
    print("=" * 70)
    print(f"Question: {question[:70]}...")
    print(f"Filter: payer={payer}")
    print(f"Blend params: n_hierarchical={n_h} n_factual={n_f} (canonical -> mostly hierarchical)")
    print(f"confidence_min: {confidence_min}")
    print()

    import urllib.request
    payload = {
        "question": question,
        "path": "mobius",
        "top_k": 10,
        "apply_google": True,
        "include_trace": True,
        "filter_payer": payer,
        "n_factual": n_f,
        "n_hierarchical": n_h,
    }
    req = urllib.request.Request(
        f"{rag_api_url.rstrip('/')}/retrieve",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        print(f"RAG API call failed: {e}")
        return 1

    docs = data.get("docs") or []
    trace = data.get("retrieval_trace") or {}

    print("-" * 50)
    print("STAGE 1: RETRIEVAL (BM25 + Vector)")
    print("-" * 50)
    ext = trace.get("extract") or {}
    bm25_n = ext.get('bm25_raw_n', '?')
    print(f"  bm25_raw_n:      {bm25_n}")
    if "bm25_postgres_url_set" in ext:
        print(f"  bm25_postgres_url_set: {ext.get('bm25_postgres_url_set')}")
    for line in ext.get("bm25_emits") or []:
        print(f"  bm25: {line}")
    if bm25_n == 0:
        print("  >>> BM25=0? Run: python mobius-chat/scripts/trace_bm25_diagnostic.py \"<q>\" --payer \"Sunshine Health\"")
    print(f"  vector_raw_n:    {ext.get('vector_raw_n', '?')}")
    print(f"  vector_filtered_n: {ext.get('vector_filtered_n', '?')} (after abstention cutoff)")
    print(f"  merged_n:        {ext.get('merged_n', '?')}")
    print()

    print("-" * 50)
    print("STAGE 2: RERANK")
    print("-" * 50)
    rr = trace.get("rerank") or {}
    print(f"  n_chunks_input:  {rr.get('n_chunks_input', '?')}")
    print(f"  n_chunks_after_decay: {rr.get('n_chunks_after_decay', '?')}")
    print()

    print("-" * 50)
    print("STAGE 3: ASSEMBLE (blend selection)")
    print("-" * 50)
    bs = trace.get("blend_selection") or {}
    print(f"  chunks_input_n:     {bs.get('chunks_input_n', '?')}")
    print(f"  n_sentence_level_pool: {bs.get('n_sentence_level_pool', '?')}")
    print(f"  n_paragraph_level_pool: {bs.get('n_paragraph_level_pool', '?')}")
    print(f"  n_factual (sentence top-k):   {bs.get('n_factual', '?')}")
    print(f"  n_hierarchical (paragraph top-k): {bs.get('n_hierarchical', '?')}")
    print(f"  n_output (sent to confidence/Google): {bs.get('n_output', '?')}")
    print()

    print("-" * 50)
    print("STAGE 4: CONFIDENCE + filter_abstain (in assemble)")
    print("-" * 50)
    print(f"  n_assembled (final): {trace.get('n_assembled', '?')}")
    print(f"  n_corpus: {trace.get('n_corpus', '?')}  n_google: {trace.get('n_google', '?')}")
    print()

    print("-" * 50)
    print("FINAL: docs returned to chat")
    print("-" * 50)
    print(f"  n_docs: {len(docs)}")
    for i, d in enumerate(docs[:5]):
        txt = (d.get("text") or "")[:100].replace("\n", " ")
        src = d.get("retrieval_source", "?")
        conf = d.get("rerank_score") or d.get("confidence") or d.get("match_score")
        cval = float(conf) if conf is not None else 0.0
        print(f"  [{i+1}] {src} conf={cval:.3f} {txt}...")
    print()

    # Check mobius-chat's confidence_min filter (applied AFTER RAG API)
    if confidence_min and docs:
        kept = [d for d in docs if (d.get("match_score") or d.get("confidence") or 0) >= confidence_min]
        dropped = len(docs) - len(kept)
        print("-" * 50)
        print("CHAT-SIDE: confidence_min filter (non_patient_rag)")
        print("-" * 50)
        print(f"  confidence_min: {confidence_min}")
        print(f"  after filter: {len(kept)} kept, {dropped} dropped")
    print()

    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
