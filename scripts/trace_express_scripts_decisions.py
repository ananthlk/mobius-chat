#!/usr/bin/env python3
"""Trace every decision for Express Scripts question: retrieval → normalization → confidence → Google fallback.

Run: PYTHONPATH=mobius-chat uv run python mobius-chat/scripts/trace_express_scripts_decisions.py

Unpacks:
1. Retrieval: raw BM25 scores, provision_type, apply_normalize_bm25 → match_score
2. Doc assembly: DocAssemblyConfig thresholds, assign_confidence per chunk
3. best_score, apply_google_fallback branch (corpus only / complement / Google only)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

_root = CHAT_ROOT.parent
for env_path in (CHAT_ROOT / ".env", _root / ".env", _root / "mobius-config" / ".env"):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        break

QUESTION = "What phone number do pharmacies call for Express Scripts help desk?"


def main() -> int:
    print("=" * 80)
    print("TRACE: Express Scripts question – every decision unpacked")
    print("=" * 80)
    print(f"\nQuestion: {QUESTION}\n")

    db_url = os.environ.get("CHAT_RAG_DATABASE_URL", "").strip()
    if not db_url:
        print("CHAT_RAG_DATABASE_URL not set.")
        return 1

    # --- 1. Retrieval ---
    print("=" * 80)
    print("STEP 1: RETRIEVAL (mobius-retriever BM25)")
    print("=" * 80)

    from app.services.retriever_backend import retrieve_for_chat

    emitted: list[str] = []

    def emit(msg: str) -> None:
        s = (msg or "").strip()
        if s:
            emitted.append(s)

    chunks, _ = retrieve_for_chat(
        question=QUESTION,
        top_k=15,
        database_url=db_url,
        emitter=emit,
    )

    print(f"Retrieved {len(chunks)} chunks.")
    print("\nRetrieval emitted:")
    for m in emitted:
        print(f"  {m}")

    if not chunks:
        print("\nNo chunks; pipeline would use Google-only path.")
        return 0

    # --- 2. Per-chunk: raw → match_score (from retriever_backend logic) ---
    print("\n" + "=" * 80)
    print("STEP 2: NORMALIZATION (raw_score → match_score)")
    print("=" * 80)

    try:
        from mobius_retriever.config import apply_normalize_bm25, load_bm25_sigmoid_config
        bm25_cfg = load_bm25_sigmoid_config()
        print(f"bm25_sigmoid config loaded: {bool(bm25_cfg)}")
        if bm25_cfg:
            print(f"  provision_types: {list((bm25_cfg.get('provision_types') or {}).keys())}")
    except Exception as e:
        print(f"Could not load BM25 config: {e}")
        bm25_cfg = None

    print("\nPer-chunk (first 12):")
    print(f"{'#':>3} | {'raw_score':>10} | {'prov':>8} | {'match_score':>11} | label (pre-assign) | Snippet")
    print("-" * 100)

    for i, c in enumerate(chunks[:12], 1):
        raw = c.get("raw_score")
        pt = c.get("provision_type", "sentence")
        if raw is not None and bm25_cfg:
            ms = apply_normalize_bm25(float(raw), pt, bm25_cfg)
        elif raw is not None:
            ms = min(1.0, float(raw) / 50.0)
        else:
            ms = c.get("match_score") or c.get("similarity") or 0.0
        raw_s = f"{raw:.2f}" if raw is not None else "—"
        ms_s = f"{ms:.4f}" if ms is not None else "—"
        snip = (c.get("text") or "")[:50].replace("\n", " ")
        if len(snip) >= 50:
            snip += "..."
        print(f"{i:>3} | {raw_s:>10} | {pt:>8} | {ms_s:>11} |                  | {snip}")

    # --- 3. Doc assembly config ---
    print("\n" + "=" * 80)
    print("STEP 3: DOC ASSEMBLY CONFIG (DocAssemblyConfig)")
    print("=" * 80)

    from app.services.doc_assembly import DocAssemblyConfig, assign_confidence, best_score, apply_google_fallback

    cfg = DocAssemblyConfig()
    print(f"  confidence_abstain_max:      {cfg.confidence_abstain_max}  → score < this = abstain")
    print(f"  confidence_process_confident_min: {cfg.confidence_process_confident_min}  → score >= this = process_confident")
    print(f"  google_fallback_low_match_min:    {cfg.google_fallback_low_match_min}  → best in [this, 0.85) = corpus + Google complement")
    print(f"  [0.5, 0.85) = process_with_caution")

    # --- 4. assign_confidence per chunk ---
    print("\n" + "=" * 80)
    print("STEP 4: assign_confidence (per chunk)")
    print("=" * 80)

    print(f"\nDecision: score < {cfg.confidence_abstain_max} → abstain")
    print(f"          score in [{cfg.confidence_abstain_max}, {cfg.confidence_process_confident_min}) → process_with_caution")
    print(f"          score >= {cfg.confidence_process_confident_min} → process_confident\n")

    labeled = []
    for i, c in enumerate(chunks[:12], 1):
        out = assign_confidence(dict(c), cfg)
        labeled.append(out)
        score = out.get("rerank_score") or out.get("match_score") or 0.0
        label = out.get("confidence_label", "?")
        snip = (out.get("text") or "")[:45].replace("\n", " ")
        print(f"  [{i}] score={score:.4f} → {label:22} | {snip}...")

    # Use full labeled set for best_score (assign_confidence was already applied in retriever output via match_score)
    chunks_for_best = [assign_confidence(dict(c), cfg) for c in chunks]
    best = best_score(chunks_for_best)

    print(f"\n  best_score = {best:.4f}")

    # --- 5. apply_google_fallback branch ---
    print("\n" + "=" * 80)
    print("STEP 5: apply_google_fallback (branch)")
    print("=" * 80)

    print(f"\n  best = {best:.4f}")
    print(f"  Branch:")
    if best >= cfg.confidence_process_confident_min:
        print(f"    best >= {cfg.confidence_process_confident_min} → 'Corpus confidence sufficient; using retrieved docs only.'")
    elif best >= cfg.google_fallback_low_match_min:
        print(f"    best in [{cfg.google_fallback_low_match_min}, {cfg.confidence_process_confident_min}) → 'Adding external search to complement corpus...'")
    else:
        print(f"    best < {cfg.google_fallback_low_match_min} → 'Low corpus confidence; using external search.'")

    # Run it to get the message
    fallback_emitted: list[str] = []
    out = apply_google_fallback(chunks, QUESTION, config=cfg, emitter=fallback_emitted.append)
    print(f"\n  Actual message: {fallback_emitted[0] if fallback_emitted else '(none)'}")
    print(f"  Chunks sent to LLM: {len(out)}")

    # --- 6. Threshold sanity ---
    print("\n" + "=" * 80)
    print("STEP 6: THRESHOLD OBSERVATION")
    print("=" * 80)

    scores = [c.get("match_score") or c.get("confidence") or 0.0 for c in chunks]
    min_s, max_s = min(scores), max(scores)
    above_85 = sum(1 for s in scores if s >= 0.85)
    below_50 = sum(1 for s in scores if s < 0.5)
    print(f"  match_score range: [{min_s:.4f}, {max_s:.4f}]")
    print(f"  Chunks with score >= 0.85 (process_confident): {above_85}/{len(chunks)}")
    print(f"  Chunks with score < 0.5 (abstain): {below_50}/{len(chunks)}")
    print(f"\n  → If BM25 sigmoid produces very high scores for in-corpus matches, almost everything is process_confident.")
    print(f"  → Out-of-syllabus questions: retrieval still returns 'best lexical matches' which may have moderate raw_scores.")
    print(f"  → Sigmoid could map those to > 0.5, so we never hit abstain/Google-only for OOS.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
