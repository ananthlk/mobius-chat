#!/usr/bin/env python3
"""pgvector sign-off: absolute quality battery (replaces parity test).

The Chroma → pgvector migration was originally going to gate on parity
(top-K overlap). That fell apart for two reasons:

1. Chroma's e2-micro VM can't reliably service the over-fetch-then-
   filter query needed to drop phantoms. ``?store=chroma_filtered``
   times out at 30s.
2. Chroma was returning phantom hits — document_ids deleted from
   Postgres but never cleaned from the Chroma index — making
   raw-Chroma the wrong baseline.

The rag agent and I converged on this absolute quality test instead.
pgvector is the source of truth (it queries Postgres directly, can't
return phantoms by construction), and we just need to confirm:

  1. **Phantom-free**: every document_id in pgvector results exists
     in Postgres. (Should be 100% by construction; we verify.)
  2. **Latency**: p99 < 200ms across a representative load.
  3. **Non-empty + score-sanity**: no zero-result queries, top-similarity
     values look plausible (variance > some floor; not all zeros, not
     all 1.0s).

If all three pass, sign-off lands and the rag agent flips
VECTOR_STORE=pgvector for the production cutover.

Usage:
    python scripts/bench_pgvector_signoff.py
    python scripts/bench_pgvector_signoff.py --trials 5    # latency stability
    python scripts/bench_pgvector_signoff.py --out /tmp/signoff.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx


RAG_URL = "https://mobius-rag-ortabkknqa-uc.a.run.app"
RAG_PROJECT = "mobius-os-dev"
RAG_SECRET = "rag-admin-api-key"


# Same query set as bench_vector_parity.py — representative payer-ops
# spread across PA / appeals / claims / pharmacy / credentialing /
# behavioral health / EPSDT.
DEFAULT_QUERIES: list[str] = [
    "prior authorization timeline",
    "denial appeals timely filing",
    "pharmacy benefit limits",
    "credentialing requirements",
    "claims submission process",
    "EPSDT requirements adolescent",
    "split residential stay billing",
    "provider dispute form",
    "duplicate denial CO-186",
    "fair hearing escalation",
]


@dataclass
class QueryResult:
    query: str
    latency_ms: float
    document_ids: list[str]
    similarity_scores: list[float]
    err: str | None = None


def _read_admin_key() -> str:
    env_key = (os.environ.get("MOBIUS_RAG_ADMIN_KEY") or "").strip()
    if env_key:
        return env_key
    try:
        out = subprocess.check_output(
            ["gcloud", "secrets", "versions", "access", "latest",
             f"--secret={RAG_SECRET}", f"--project={RAG_PROJECT}"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        sys.stderr.write(
            f"Could not read secret {RAG_SECRET!r} via gcloud: {e}\n"
            "Set MOBIUS_RAG_ADMIN_KEY env var as a fallback.\n"
        )
        sys.exit(2)


def _query_pgvector(client: httpx.Client, query: str, k: int, admin_key: str) -> QueryResult:
    t0 = time.perf_counter()
    try:
        r = client.get(
            f"{RAG_URL}/admin/vector_search",
            params={"q": query, "store": "pgvector", "k": k},
            headers={"X-Admin-Key": admin_key},
            timeout=15,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            return QueryResult(query, elapsed_ms, [], [], err=f"HTTP {r.status_code}")
        body = r.json()
        results = body.get("results") or []
        dids = [str(item.get("document_id") or "") for item in results if item.get("document_id")]
        # pgvector returns "distance" but it's actually similarity (1 = identical).
        # See rag agent's contract note: polarity is reversed between stores.
        sims = [float(item.get("distance") or 0.0) for item in results]
        return QueryResult(query, elapsed_ms, dids, sims)
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return QueryResult(query, elapsed_ms, [], [], err=f"{type(e).__name__}: {e}")


def _check_document_exists(client: httpx.Client, doc_id: str, admin_key: str) -> bool:
    """Phantom check — does this document_id exist in Postgres?

    Uses /documents/{id}/detail which returns 200 for live docs, 404
    for missing/deleted. Auth header included even though some endpoints
    don't require it; harmless if ignored.
    """
    try:
        r = client.get(
            f"{RAG_URL}/documents/{doc_id}/detail",
            headers={"X-Admin-Key": admin_key},
            timeout=10,
        )
        return r.status_code == 200
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=5, help="top-K to retrieve per query (default 5)")
    ap.add_argument("--trials", type=int, default=3, help="latency trials per query (default 3)")
    ap.add_argument("--queries-file", help="text file with one query per line")
    ap.add_argument("--out", help="write JSON report to this path")
    ap.add_argument("--latency-p99-ms", type=int, default=200,
                    help="latency p99 threshold (default 200)")
    args = ap.parse_args()

    queries = DEFAULT_QUERIES
    if args.queries_file:
        with open(args.queries_file) as f:
            queries = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    admin_key = _read_admin_key()
    print(f"=== pgvector sign-off battery ===")
    print(f"endpoint: {RAG_URL}/admin/vector_search?store=pgvector")
    print(f"queries: {len(queries)}    k={args.k}    trials={args.trials}")
    print(f"thresholds: latency_p99 < {args.latency_p99_ms}ms, phantom_rate = 0%, no empty results")
    print()

    all_latencies: list[float] = []
    all_results: list[QueryResult] = []
    all_unique_doc_ids: set[str] = set()
    empty_queries: list[str] = []
    score_distribution: list[list[float]] = []

    with httpx.Client() as client:
        # ── Stage 1: run queries × trials, collect latencies + result sets ──
        print("Stage 1: query latency + retrieval")
        for q in queries:
            trial_latencies: list[float] = []
            last_result: QueryResult | None = None
            for t in range(args.trials):
                r = _query_pgvector(client, q, args.k, admin_key)
                trial_latencies.append(r.latency_ms)
                last_result = r
            assert last_result is not None
            all_latencies.extend(trial_latencies)
            all_results.append(last_result)
            for did in last_result.document_ids:
                all_unique_doc_ids.add(did)
            if last_result.similarity_scores:
                score_distribution.append(last_result.similarity_scores)
            if not last_result.document_ids:
                empty_queries.append(q)
            avg_lat = statistics.mean(trial_latencies)
            print(f"  {q[:42]:42s}  avg={avg_lat:5.0f}ms  results={len(last_result.document_ids)}/{args.k}  "
                  f"top-sim={last_result.similarity_scores[0] if last_result.similarity_scores else 0.0:.3f}")

        # ── Stage 2: phantom check ─────────────────────────────────────
        print()
        print("Stage 2: phantom check (verify each unique document_id exists in Postgres)")
        phantoms: list[str] = []
        verified: list[str] = []
        for did in sorted(all_unique_doc_ids):
            ok = _check_document_exists(client, did, admin_key)
            if ok:
                verified.append(did)
            else:
                phantoms.append(did)
        print(f"  unique doc_ids checked: {len(all_unique_doc_ids)}")
        print(f"  verified: {len(verified)}")
        print(f"  phantoms: {len(phantoms)}")
        if phantoms:
            for p in phantoms[:10]:
                print(f"    PHANTOM → {p}")

    # ── Stage 3: aggregate + verdict ─────────────────────────────────
    print()
    print("─" * 65)
    p50 = statistics.median(all_latencies)
    p95 = sorted(all_latencies)[max(0, int(0.95 * len(all_latencies)) - 1)]
    p99 = sorted(all_latencies)[max(0, int(0.99 * len(all_latencies)) - 1)]
    print(f"Latency  p50/p95/p99: {p50:.0f}ms / {p95:.0f}ms / {p99:.0f}ms     (target p99 <{args.latency_p99_ms}ms)")
    print(f"Empty queries: {len(empty_queries)}/{len(queries)}                 (target 0)")
    print(f"Phantom doc_ids: {len(phantoms)}/{len(all_unique_doc_ids)}                (target 0)")

    # Score-sanity: variance of TOP similarity across queries should be > floor
    top_sims = [scores[0] for scores in score_distribution if scores]
    if len(top_sims) >= 2:
        score_variance = statistics.stdev(top_sims)
    else:
        score_variance = 0.0
    print(f"Top-sim variance across queries: {score_variance:.4f}    (target >0.005 — flags 'all zeros' or 'all 1.0' degenerate cases)")
    print(f"Top-sim range: {min(top_sims):.3f} → {max(top_sims):.3f}")

    print()
    failures: list[str] = []
    if p99 > args.latency_p99_ms:
        failures.append(f"latency p99 {p99:.0f}ms exceeds {args.latency_p99_ms}ms threshold")
    if empty_queries:
        failures.append(f"{len(empty_queries)} queries returned zero results: {empty_queries}")
    if phantoms:
        failures.append(f"{len(phantoms)} phantom document_ids returned (target 0)")
    if score_variance < 0.005:
        failures.append(f"top-similarity variance {score_variance:.4f} too low — distribution looks degenerate")

    if not failures:
        print("✓ SIGN-OFF: all three checks pass")
        print("  pgvector backend is ready for production cutover (Step 5).")
    else:
        print("⚠ FAIL — sign-off blocked:")
        for f in failures:
            print(f"  - {f}")

    if args.out:
        with open(args.out, "w") as f:
            json.dump({
                "endpoint": RAG_URL,
                "k": args.k,
                "trials": args.trials,
                "queries": queries,
                "results": [
                    {"query": r.query, "latency_ms": r.latency_ms,
                     "document_ids": r.document_ids,
                     "similarity_scores": r.similarity_scores,
                     "err": r.err}
                    for r in all_results
                ],
                "summary": {
                    "latency_p50_ms": p50,
                    "latency_p95_ms": p95,
                    "latency_p99_ms": p99,
                    "empty_queries": empty_queries,
                    "phantom_doc_ids": phantoms,
                    "verified_doc_ids_count": len(verified),
                    "top_sim_variance": score_variance,
                    "top_sim_range": [min(top_sims) if top_sims else None,
                                      max(top_sims) if top_sims else None],
                    "passed": len(failures) == 0,
                    "failures": failures,
                },
            }, f, indent=2)
        print(f"\nwrote report → {args.out}")

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
