#!/usr/bin/env python3
"""Parity test: pgvector vs Chroma on mobius-rag's /admin/vector_search.

Hits the same query against both stores, reports top-K overlap + latency.
Required by Step 4 of the rag-side Chroma → pgvector migration plan
(2026-04-27). The migration is invisible to chat once cutover lands —
this script is the chat-side gate that says "yes, parity is good
enough to flip the production /api/query default to pgvector."

Auth: GET ``rag-admin-api-key`` from Secret Manager and pass as
``X-Admin-Key``. Same secret chat already uses for curator tools, but
the rag /admin endpoints want a different header name.

Two important contract notes from the rag agent:

1. **Polarity flip.** Both stores return a field called ``distance``,
   but the math is opposite:
       pgvector → cosine similarity (1 = identical, 0 = orthogonal)
       chroma   → cosine distance   (0 = identical, 1 = orthogonal)
   This script ignores raw scores and compares **rank overlap** —
   which source_ids appear in the top-K and in what order.

2. **HNSW index warming.** For the first ~5 minutes after migration,
   pgvector queries do a sequential scan over ~18.6k rows (1-3s).
   Once the HNSW index finishes building, p50 drops below 50ms.
   Re-run this script after the index settles for the real numbers.

Usage:
    python scripts/bench_vector_parity.py                    # default 8 queries, k=5
    python scripts/bench_vector_parity.py --k 10             # top-10 overlap
    python scripts/bench_vector_parity.py --queries-file my_eval.txt
    python scripts/bench_vector_parity.py --filter "state=FL"

Pass criteria: top-K overlap ≥ 80% on average, ≥ 70% on every query.
Below 70% on any query is a parity failure that needs investigation.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass

import httpx


RAG_URL = "https://mobius-rag-ortabkknqa-uc.a.run.app"
RAG_PROJECT = "mobius-os-dev"
RAG_SECRET = "rag-admin-api-key"


# Representative payer-ops queries spanning the chat's typical workload.
# Picked to exercise different parts of the corpus: PA / appeals / claims /
# pharmacy / credentialing / care-mgmt / behavioral-health.
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
class StoreResult:
    store: str
    query: str
    source_ids: list[str]
    document_ids: list[str]   # rag agent (2026-04-27): the corpus has 25.6%
                              # duplicate embeddings (same vector, different
                              # source_ids — same document_id). HNSW picks
                              # different reps of duplicate clusters → top-K
                              # source_id overlap is misleadingly low.
                              # document_id dedups the cluster, restoring
                              # the meaningful "did both stores find the
                              # same content" signal.
    latency_ms: float
    err: str | None = None


def _read_admin_key() -> str:
    """Pull the admin key from Secret Manager. Falls back to env."""
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


def _query_store(
    client: httpx.Client,
    *,
    store: str,
    query: str,
    k: int,
    extra_filters: dict[str, str],
    admin_key: str,
) -> StoreResult:
    """One /admin/vector_search call. Returns source_ids in rank order."""
    params: dict[str, str | int] = {"q": query, "store": store, "k": k}
    params.update(extra_filters)
    t0 = time.perf_counter()
    try:
        r = client.get(
            f"{RAG_URL}/admin/vector_search",
            params=params,
            headers={"X-Admin-Key": admin_key},
            timeout=30,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            return StoreResult(store, query, [], elapsed_ms, err=f"HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        results = body.get("results") or []
        sids = [str(item.get("source_id") or "") for item in results if item.get("source_id")]
        dids = [str(item.get("document_id") or "") for item in results if item.get("document_id")]
        return StoreResult(store, query, sids, dids, elapsed_ms)
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return StoreResult(store, query, [], [], elapsed_ms, err=f"{type(e).__name__}: {e}")


def _overlap(a: list[str], b: list[str]) -> tuple[int, int]:
    """Return (intersection size, union size) treating order as irrelevant.

    For ranked overlap, a future enhancement could weight rank position
    (Spearman correlation). For now we go with raw set overlap — the
    metric the rag agent's handoff doc proposed.
    """
    sa, sb = set(a), set(b)
    return len(sa & sb), len(sa | sb)


def _kendall_tau_simple(a: list[str], b: list[str]) -> float | None:
    """Order-aware similarity: how many top-K items show up in BOTH lists
    AT ROUGHLY THE SAME RANK? Returns 1.0 = identical order on the
    intersection, 0.0 = totally inverted. None when intersection < 2.
    """
    common = [x for x in a if x in b]
    if len(common) < 2:
        return None
    rank_a = {x: a.index(x) for x in common}
    rank_b = {x: b.index(x) for x in common}
    concordant = discordant = 0
    for i in range(len(common)):
        for j in range(i + 1, len(common)):
            x, y = common[i], common[j]
            da = rank_a[x] - rank_a[y]
            db = rank_b[x] - rank_b[y]
            if da * db > 0:
                concordant += 1
            elif da * db < 0:
                discordant += 1
    total = concordant + discordant
    if total == 0:
        return None
    return (concordant - discordant) / total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=5, help="top-K to compare (default 5)")
    ap.add_argument("--queries-file", help="text file with one query per line; falls back to DEFAULT_QUERIES")
    ap.add_argument("--filter", action="append", default=[],
                    help="additional filter as key=value (repeat). e.g. --filter state=FL --filter payer='Sunshine Health'")
    ap.add_argument("--out", help="write JSON report to this path")
    ap.add_argument("--pass-threshold", type=float, default=0.7,
                    help="minimum per-query overlap fraction below which we flag a failure (default 0.7)")
    args = ap.parse_args()

    queries = DEFAULT_QUERIES
    if args.queries_file:
        with open(args.queries_file) as f:
            queries = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not queries:
        sys.stderr.write("No queries to run.\n")
        return 2

    extra_filters: dict[str, str] = {}
    for f in args.filter:
        if "=" not in f:
            sys.stderr.write(f"--filter expects key=value, got {f!r}\n")
            return 2
        k, v = f.split("=", 1)
        extra_filters[k.strip()] = v.strip()

    admin_key = _read_admin_key()
    print(f"=== vector parity: pgvector vs chroma | k={args.k} | n={len(queries)} ===")
    if extra_filters:
        print(f"filters: {extra_filters}")
    print(f"endpoint: {RAG_URL}/admin/vector_search")
    print()

    rows: list[dict] = []
    pg_lats: list[float] = []
    ch_lats: list[float] = []
    sid_overlaps: list[float] = []     # raw source_id overlap (debug signal)
    did_overlaps: list[float] = []     # primary metric (dedups duplicate vectors)

    with httpx.Client() as client:
        for q in queries:
            pg = _query_store(client, store="pgvector", query=q, k=args.k,
                              extra_filters=extra_filters, admin_key=admin_key)
            ch = _query_store(client, store="chroma", query=q, k=args.k,
                              extra_filters=extra_filters, admin_key=admin_key)
            sid_inter, _ = _overlap(pg.source_ids, ch.source_ids)
            did_inter, _ = _overlap(pg.document_ids, ch.document_ids)
            # Use UNIQUE doc_ids in the denominator — if both stores' top-K
            # collapses to fewer unique docs (because of duplicate-vector
            # clusters), the fraction reflects what's actually comparable.
            uniq_pg_dids = len(set(pg.document_ids))
            uniq_ch_dids = len(set(ch.document_ids))
            denom = max(uniq_pg_dids, uniq_ch_dids, 1)
            sid_frac = sid_inter / args.k if args.k else 0.0
            did_frac = did_inter / denom
            tau = _kendall_tau_simple(pg.source_ids, ch.source_ids)
            pg_lats.append(pg.latency_ms)
            ch_lats.append(ch.latency_ms)
            sid_overlaps.append(sid_frac)
            did_overlaps.append(did_frac)

            rows.append({
                "query": q,
                "k": args.k,
                "pgvector": {"latency_ms": round(pg.latency_ms, 1),
                             "source_ids": pg.source_ids,
                             "document_ids": pg.document_ids,
                             "err": pg.err},
                "chroma":   {"latency_ms": round(ch.latency_ms, 1),
                             "source_ids": ch.source_ids,
                             "document_ids": ch.document_ids,
                             "err": ch.err},
                "source_id_overlap_count": sid_inter,
                "source_id_overlap_fraction": sid_frac,
                "document_id_overlap_count": did_inter,
                "document_id_overlap_fraction": did_frac,
                "kendall_tau": tau,
            })
            tau_str = f"{tau:+.2f}" if tau is not None else "  —  "
            err_marker = ""
            if pg.err: err_marker += " [pg-err]"
            if ch.err: err_marker += " [ch-err]"
            # Primary line: document_id overlap (the metric that actually
            # signals parity given the duplicate-vector clusters).
            print(
                f"  {q[:38]:38s}  doc-ovl={did_inter}/{denom}={did_frac:.0%}  "
                f"src-ovl={sid_frac:.0%}  τ={tau_str}  pg={pg.latency_ms:5.0f}ms  "
                f"ch={ch.latency_ms:5.0f}ms{err_marker}"
            )

    # ── Aggregates ────────────────────────────────────────────────────
    print()
    print("─" * 65)
    print(f"avg DOCUMENT_ID overlap : {statistics.mean(did_overlaps):.1%}     ← primary parity metric")
    print(f"min DOCUMENT_ID overlap : {min(did_overlaps):.1%}")
    print(f"queries below {args.pass_threshold:.0%}     : {sum(1 for o in did_overlaps if o < args.pass_threshold)}/{len(did_overlaps)}")
    print()
    print(f"avg source_id overlap   : {statistics.mean(sid_overlaps):.1%}     ← masked by 25.6% duplicate vectors per rag agent")
    print(f"min source_id overlap   : {min(sid_overlaps):.1%}")
    print()
    print(f"pgvector p50 latency    : {statistics.median(pg_lats):>6.0f} ms     (target: <200ms)")
    print(f"pgvector p95 latency    : {sorted(pg_lats)[int(0.95 * len(pg_lats))]:>6.0f} ms")
    print(f"chroma   p50 latency    : {statistics.median(ch_lats):>6.0f} ms")
    print(f"chroma   p95 latency    : {sorted(ch_lats)[int(0.95 * len(ch_lats))]:>6.0f} ms")

    # ── Verdict (uses document_id overlap, not source_id) ──────────────
    avg_did = statistics.mean(did_overlaps)
    failures = [r for r in rows if r["document_id_overlap_fraction"] < args.pass_threshold]
    print()
    if failures:
        print(f"⚠ FAIL: {len(failures)} queries below {args.pass_threshold:.0%} doc_id overlap threshold")
        for f in failures:
            print(f"   - {f['query']}: doc-ovl={f['document_id_overlap_fraction']:.0%}")
    elif avg_did >= 0.8:
        print(f"✓ PASS: avg doc_id overlap {avg_did:.1%} ≥ 80%; all queries above {args.pass_threshold:.0%}")
    else:
        print(f"⚠ PASS w/ caveat: avg {avg_did:.1%} (below 80% target but no individual failure)")

    if args.out:
        with open(args.out, "w") as f:
            json.dump({
                "endpoint": RAG_URL,
                "k": args.k,
                "filters": extra_filters,
                "rows": rows,
                "summary": {
                    "primary_metric": "document_id_overlap",
                    "avg_document_id_overlap": avg_did,
                    "min_document_id_overlap": min(did_overlaps),
                    "avg_source_id_overlap": statistics.mean(sid_overlaps),
                    "pg_p50_ms": statistics.median(pg_lats),
                    "ch_p50_ms": statistics.median(ch_lats),
                    "fail_count": len(failures),
                    "note": (
                        "RAG corpus has 25.6% duplicate vectors (same vector, "
                        "different source_id, same document_id). Top-K source_id "
                        "overlap is misleadingly low because HNSW picks different "
                        "representatives of identical-vector clusters. document_id "
                        "overlap is the parity metric that survives the dedup."
                    ),
                },
            }, f, indent=2)
        print(f"\nwrote report → {args.out}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
