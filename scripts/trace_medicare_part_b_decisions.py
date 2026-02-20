#!/usr/bin/env python3
"""Trace every decision for Medicare Part B (out-of-syllabus) question.

Expect: low scores, abstain or Google-only. If we see process_confident, thresholds need adjustment.

Run: PYTHONPATH=mobius-chat uv run python mobius-chat/scripts/trace_medicare_part_b_decisions.py
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

QUESTION = "What is the Medicare Part B prior authorization process in California?"


def main() -> int:
    print("=" * 80)
    print("TRACE: Medicare Part B (OUT OF SYLLABUS) – every decision unpacked")
    print("=" * 80)
    print(f"\nQuestion: {QUESTION}")
    print("expect_in_manual: FALSE (Sunshine Health manual has Medicaid/Florida, not Medicare Part B/California)\n")

    db_url = os.environ.get("CHAT_RAG_DATABASE_URL", "").strip()
    if not db_url:
        print("CHAT_RAG_DATABASE_URL not set.")
        return 1

    from app.services.retriever_backend import retrieve_for_chat
    from app.services.doc_assembly import DocAssemblyConfig, assign_confidence, best_score, apply_google_fallback

    emitted: list[str] = []
    chunks, _ = retrieve_for_chat(
        question=QUESTION,
        top_k=15,
        database_url=db_url,
        emitter=emitted.append,
    )

    print("STEP 1: RETRIEVAL")
    print("-" * 40)
    for m in emitted:
        print(f"  {m}")
    print(f"\nRetrieved {len(chunks)} chunks.")

    if not chunks:
        print("\nbest_score = 0 (no chunks) → Google-only path expected.")
        return 0

    cfg = DocAssemblyConfig()
    chunks_labeled = [assign_confidence(dict(c), cfg) for c in chunks]
    best = best_score(chunks_labeled)

    print("\nSTEP 2: Per-chunk scores (first 10)")
    print("-" * 40)
    print(f"{'#':>3} | raw_score | match_score | label            | Snippet")
    for i, c in enumerate(chunks[:10], 1):
        raw = c.get("raw_score")
        ms = c.get("match_score") or c.get("confidence") or 0.0
        label = chunks_labeled[i - 1].get("confidence_label", "?") if i <= len(chunks_labeled) else "?"
        snip = (c.get("text") or "")[:50].replace("\n", " ")
        print(f"{i:>3} | {raw:>8.2f} | {ms:>11.4f} | {label:16} | {snip}...")

    scores = [c.get("match_score") or c.get("confidence") or 0.0 for c in chunks]
    print(f"\n  best_score = {best:.4f}")
    print(f"  score range: [{min(scores):.4f}, {max(scores):.4f}]")
    below_50 = sum(1 for s in scores if s < 0.5)
    above_85 = sum(1 for s in scores if s >= 0.85)
    print(f"  chunks < 0.5 (abstain): {below_50}")
    print(f"  chunks >= 0.85 (process_confident): {above_85}")

    fallback_emitted: list[str] = []
    out = apply_google_fallback(chunks, QUESTION, config=cfg, emitter=fallback_emitted.append)
    print(f"\n  apply_google_fallback message: {fallback_emitted[0] if fallback_emitted else '(none)'}")
    print(f"  Chunks sent to LLM: {len(out)}")

    print("\n" + "=" * 80)
    print("EXPECTATION vs ACTUAL")
    print("=" * 80)
    print("  Expected: abstain or Google-only (question is out of syllabus)")
    print(f"  Actual best_score: {best:.4f}")
    if best >= 0.85:
        print("  → PROBLEM: best >= 0.85 → process_confident. Out-of-syllabus gets high scores from BM25.")
    elif best >= 0.5:
        print("  → best in [0.5, 0.85) → corpus + Google complement. Lexical matches still scoring moderate.")
    else:
        print("  → best < 0.5 → Google-only. Correct behavior for OOS.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
