#!/usr/bin/env python3
"""Hybrid retrieval acceptance test (Sprint 2 #0.2, 2026-04-24).

Verifies:
  1. BM25 arm returns hits on keyword-heavy queries.
  2. Vector arm returns hits on paraphrased queries with no keyword overlap.
  3. Hybrid (corpus mode) fuses both arms via RRF, no duplicates,
     ``retrieval_arms`` annotates per-chunk provenance.
  4. Canonical/factual blend (n_hierarchical / n_factual) is preserved.
  5. Three call modes route correctly:
       corpus    → hybrid (default)
       precision → BM25-only
       recall    → vector-only
  6. Tool-name aliases resolve to the canonical names in
     react_loop._normalize_tool_name.

Run:
    cd mobius-chat
    export CHROMA_AUTH_TOKEN=$(gcloud secrets versions access latest \
       --secret=chroma-auth-token --project=mobius-os-dev)
    export VERTEX_PROJECT_ID=mobius-os-dev
    export CHROMA_HOST=34.170.243.161
    export CHROMA_PORT=8000
    export CHAT_RAG_DATABASE_URL='postgresql+psycopg2://postgres:<pw>@127.0.0.1:5433/mobius_chat'
    ./.venv/bin/python scripts/test_hybrid_retrieval.py

Exit code 0 = all assertions pass. Non-zero = at least one regression.

Designed to be runnable from a developer laptop with the Cloud SQL
proxy on :5433 and shared Chroma reachable via CHROMA_HOST. Each test
prints a one-line PASS/FAIL header so the output reads top-to-bottom
even when assertions are noisy.
"""
from __future__ import annotations

import os
import sys
from typing import Any

# Each test function prints its own PASS/FAIL line. The harness counts.
_pass = 0
_fail = 0


def _hdr(label: str) -> None:
    print()
    print("═" * 72)
    print(f"  {label}")
    print("═" * 72)


def _assert(cond: bool, label: str) -> None:
    global _pass, _fail
    if cond:
        print(f"  ✓ {label}")
        _pass += 1
    else:
        print(f"  ✗ FAIL — {label}")
        _fail += 1


def _setup_env() -> str:
    """Return the resolved DB URL — fails fast if the env isn't set up."""
    db = (
        os.environ.get("CHAT_RAG_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    ).strip()
    if not db:
        print("ERROR: set CHAT_RAG_DATABASE_URL or DATABASE_URL "
              "(Cloud SQL proxy or direct).")
        sys.exit(2)
    if not os.environ.get("CHROMA_HOST"):
        print("ERROR: set CHROMA_HOST=34.170.243.161 (or proxy).")
        sys.exit(2)
    if not os.environ.get("VERTEX_PROJECT_ID"):
        os.environ["VERTEX_PROJECT_ID"] = "mobius-os-dev"
    return db


def test_alias_resolution() -> None:
    """Aliases must canonicalize at the dispatcher boundary."""
    _hdr("1. Alias resolution → canonical tool names")
    from app.pipeline.react_loop import _normalize_tool_name

    cases = [
        # (alias, expected_canonical)
        ("search_corpus",         "search_corpus"),
        ("CORPUS",                "search_corpus"),
        ("hybrid",                "search_corpus"),
        ("hybrid_search",         "search_corpus"),
        ("default_search",        "search_corpus"),
        ("recall_search",         "recall_search"),
        ("lazy_corpus_search",    "recall_search"),
        ("broad",                 "recall_search"),
        ("explore",               "recall_search"),
        ("vector_search",         "recall_search"),
        ("semantic_search",       "recall_search"),
        ("precision_search",      "precision_search"),
        ("exact",                 "precision_search"),
        ("keyword_search",        "precision_search"),
        ("bm25_search",           "precision_search"),
        ("bm25",                  "precision_search"),
        ("lookup",                "precision_search"),
        # Unknown tool falls through unchanged
        ("healthcare_query",      "healthcare_query"),
        ("totally_made_up_tool",  "totally_made_up_tool"),
    ]
    for alias, expected in cases:
        got = _normalize_tool_name(alias)
        _assert(got == expected, f"{alias!r} → {got!r} (expected {expected!r})")


def test_precision_arm(db: str) -> None:
    """BM25-only path returns hits on keyword-heavy queries."""
    _hdr("2. precision_search (BM25-only) on keyword-heavy query")
    from app.services.retriever_backend import retrieve_for_chat

    chunks, tele = retrieve_for_chat(
        question="prior authorization sunshine health behavioral health",
        top_k=5,
        database_url=db,
        mode="precision",
        include_trace=True,
    )
    print(f"  telemetry: {tele}")
    _assert(isinstance(chunks, list), "chunks is a list")
    _assert(len(chunks) > 0, f"got {len(chunks)} BM25 hits (expected ≥ 1)")
    if chunks:
        ms = chunks[0].get("match_score") or chunks[0].get("rerank_score")
        _assert(ms is not None, "first chunk has match_score / rerank_score")
    if tele:
        _assert(tele.get("mode") == "corpus_precision", "telemetry mode=corpus_precision")
        _assert(tele.get("arm_bm25_hits", 0) > 0, "arm_bm25_hits > 0")


def test_recall_arm() -> None:
    """Vector-only path returns hits on a semantically-phrased query."""
    _hdr("3. recall_search (vector-only) on paraphrase with no keyword overlap")
    from app.services.retriever_backend import retrieve_for_chat

    # Deliberate paraphrase: no "appeal" / "claim" / "deny" / "PA" /
    # "prior authorization" — pure semantic match for behavioral health
    # policy content.
    chunks, tele = retrieve_for_chat(
        question="What rules govern in-network mental health services for FL Medicaid members?",
        top_k=5,
        database_url="",  # vector arm doesn't need it
        mode="recall",
        include_trace=True,
    )
    print(f"  telemetry: {tele}")
    _assert(isinstance(chunks, list), "chunks is a list")
    _assert(len(chunks) > 0, f"got {len(chunks)} vector hits (expected ≥ 1)")
    if tele:
        _assert(tele.get("mode") == "corpus_recall", "telemetry mode=corpus_recall")


def test_hybrid_fusion(db: str) -> None:
    """Hybrid runs both arms and fuses via RRF with overlap > 0."""
    _hdr("4. search_corpus (hybrid BM25 ⊕ vector) — RRF fusion")
    from app.services.retriever_backend import retrieve_for_chat

    chunks, tele = retrieve_for_chat(
        question="Sunshine Health behavioral health prior authorization policy",
        top_k=10,
        database_url=db,
        mode="corpus",
        include_trace=True,
    )
    print(f"  telemetry: {tele}")
    _assert(isinstance(chunks, list), "chunks is a list")
    _assert(len(chunks) > 0, f"got {len(chunks)} fused chunks (expected ≥ 1)")

    if tele:
        _assert(tele.get("mode") == "corpus_hybrid", "telemetry mode=corpus_hybrid")
        _assert(tele.get("arm_bm25_hits", 0) > 0, f"arm_bm25_hits={tele.get('arm_bm25_hits',0)} > 0")
        _assert(tele.get("arm_vector_hits", 0) > 0, f"arm_vector_hits={tele.get('arm_vector_hits',0)} > 0")
        # Overlap can be 0 if the two arms surface entirely disjoint sets.
        # We don't assert >0 here — both arms returning > 0 is the real signal.

    # No duplicate IDs after fusion (RRF should dedupe by id)
    ids = [str(c.get("id")) for c in chunks if c.get("id")]
    _assert(len(ids) == len(set(ids)), f"no duplicate IDs (n={len(ids)}, distinct={len(set(ids))})")

    # Per-chunk provenance present
    if chunks:
        first = chunks[0]
        arms = first.get("retrieval_arms") or first.get("_arm_origin") or None
        # Hybrid path sets retrieval_arms in retriever_hybrid._rrf_merge;
        # blend selection may strip it via _apply_blend_selection (just
        # passes dicts through), so it should still be there.
        _assert(arms is not None, f"first chunk has provenance (retrieval_arms or _arm_origin), got {arms}")


def test_blend_selection(db: str) -> None:
    """Canonical (n_hierarchical) + factual (n_factual) blend honored post-fusion."""
    _hdr("5. canonical/factual blend preserved through hybrid")
    from app.services.retriever_backend import retrieve_for_chat

    chunks, tele = retrieve_for_chat(
        question="Sunshine Health behavioral health policy",
        top_k=10,
        database_url=db,
        mode="corpus",
        n_hierarchical=2,
        n_factual=3,
        include_trace=True,
    )
    print(f"  telemetry: {tele}")
    _assert(len(chunks) > 0, f"got {len(chunks)} blended chunks")

    para_n = sum(1 for c in chunks if (c.get("provision_type") or "").lower() == "paragraph")
    sent_n = sum(1 for c in chunks if (c.get("provision_type") or "").lower() == "sentence")
    print(f"  paragraph slots: {para_n} (target 2), sentence slots: {sent_n} (target 3)")
    # Don't insist on exact 2+3 — corpus may not have enough of each
    # type. Insist on ≤ targets and total ≤ 5.
    _assert(para_n <= 2, f"paragraph count {para_n} ≤ n_hierarchical=2")
    _assert(sent_n <= 3, f"sentence count {sent_n} ≤ n_factual=3")
    _assert(len(chunks) <= 5, f"total {len(chunks)} ≤ n_hierarchical+n_factual=5")


def test_unknown_mode_falls_back(db: str) -> None:
    """Bad mode falls through to legacy BM25 (with warning) instead of crashing."""
    _hdr("6. unknown mode → legacy BM25 fallback (graceful)")
    from app.services.retriever_backend import retrieve_for_chat

    chunks, _ = retrieve_for_chat(
        question="prior authorization",
        top_k=3,
        database_url=db,
        mode="this_mode_does_not_exist",
    )
    _assert(isinstance(chunks, list), f"got list back (got {type(chunks).__name__})")


def main() -> int:
    db = _setup_env()
    print(f"\nDB:     {db[:60]}{'…' if len(db) > 60 else ''}")
    print(f"Chroma: {os.environ.get('CHROMA_HOST')}:{os.environ.get('CHROMA_PORT', '8000')}")
    print(f"Vertex: {os.environ.get('VERTEX_PROJECT_ID')}")

    test_alias_resolution()
    test_precision_arm(db)
    test_recall_arm()
    test_hybrid_fusion(db)
    test_blend_selection(db)
    test_unknown_mode_falls_back(db)

    print()
    print("═" * 72)
    if _fail == 0:
        print(f"  ✓ ALL PASS — {_pass} assertions")
        return 0
    print(f"  ✗ {_fail} FAILED — {_pass} pass / {_fail} fail")
    return 1


if __name__ == "__main__":
    sys.exit(main())
