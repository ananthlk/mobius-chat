#!/usr/bin/env python3
"""BM25 diagnostic: trace why BM25 returns 0 (core retrieval).

Runs inline (no RAG API) to pinpoint:
  1. postgres_url set?
  2. Corpus fetch: rows with document_payer filter?
  3. document_payer values in DB (may not match filter)
  4. BM25 raw results (before cutoff)
  5. Cutoff filter (how many dropped)

The RAG API passes filter_payer → tag_filters["document_payer"]. If the DB has
different values (e.g. NULL, "Sunshine" vs "Sunshine Health"), corpus is empty → BM25=0.

Run from Mobius root:
  PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_bm25_diagnostic.py "how do I file an appeal"
  PYTHONPATH=mobius-chat python mobius-chat/scripts/trace_bm25_diagnostic.py "appeal" --payer "Sunshine Health"
"""
from __future__ import annotations

import argparse
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


def _query_distinct_payers(db_url: str) -> list[str]:
    """Return distinct non-null document_payer values in published_rag_metadata."""
    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT document_payer FROM published_rag_metadata WHERE document_payer IS NOT NULL AND document_payer != ''"
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [r[0] for r in rows if r and r[0]]
    except Exception as e:
        return [f"(query failed: {e})"]


def _query_row_counts(db_url: str, document_payer: str | None) -> tuple[int, int]:
    """(total_rows, rows_with_filter). Filter = document_payer when provided."""
    try:
        import psycopg2
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM published_rag_metadata")
        total = cur.fetchone()[0]
        if document_payer and document_payer.strip():
            cur.execute(
                "SELECT COUNT(*) FROM published_rag_metadata WHERE document_payer = %s",
                (document_payer.strip(),),
            )
            filtered = cur.fetchone()[0]
        else:
            filtered = total
        cur.close()
        conn.close()
        return total, filtered
    except Exception as e:
        return -1, -1


def main() -> int:
    parser = argparse.ArgumentParser(description="BM25 diagnostic: why is BM25 returning 0?")
    parser.add_argument("question", nargs="*", default=["how do I file an appeal"], help="Query text")
    parser.add_argument("--payer", default="Sunshine Health", help="filter_payer (matches RAG API)")
    parser.add_argument("--no-payer", action="store_true", help="Skip payer filter (full corpus)")
    parser.add_argument("--jpd", action="store_true", help="Use JPD tagger (same as RAG API path)")
    args = parser.parse_args()
    question = " ".join(args.question).strip()
    payer = "" if args.no_payer else (args.payer or "").strip()

    db_url = (os.environ.get("CHAT_RAG_DATABASE_URL") or "").strip()
    if not db_url:
        print("CHAT_RAG_DATABASE_URL not set. BM25 uses this for postgres corpus.")
        return 1

    print("=" * 70)
    print("BM25 DIAGNOSTIC")
    print("=" * 70)
    print(f"Question: {question[:60]}...")
    print(f"Filter: document_payer = {repr(payer) or '(none)'}")
    print()

    # 1. postgres_url
    print("-" * 50)
    print("1. POSTGRES_URL")
    print("-" * 50)
    print(f"  CHAT_RAG_DATABASE_URL: set ({db_url[:40]}...)")
    print()

    # 2. DB row counts and distinct payers
    print("-" * 50)
    print("2. CORPUS (published_rag_metadata)")
    print("-" * 50)
    total, filtered = _query_row_counts(db_url, payer)
    print(f"  Total rows (no filter): {total}")
    if payer:
        print(f"  Rows WHERE document_payer = {repr(payer)}: {filtered}")
        if filtered == 0 and total > 0:
            payers = _query_distinct_payers(db_url)
            print(f"  >>> No rows match. Distinct document_payer in DB: {payers}")
            print("  >>> Fix: align filter_payer with DB values, or populate document_payer.")
    else:
        print("  (No payer filter)")
    print()

    # 2b. JPD tagger (when --jpd, matches RAG API)
    document_ids: list[str] | None = None
    if args.jpd:
        print("-" * 50)
        print("2b. JPD TAGGER (use_jpd_tagger=True)")
        print("-" * 50)
        from mobius_retriever.jpd_tagger import tag_question_and_resolve_document_ids

        jpd_emitted: list[str] = []

        def jpd_emit(m: str) -> None:
            if m.strip():
                jpd_emitted.append(m.strip())

        jpd = tag_question_and_resolve_document_ids(question, db_url, emitter=jpd_emit)
        document_ids = jpd.document_ids if jpd.has_document_ids else None
        for line in jpd_emitted:
            print(f"  {line}")
        print(f"  document_ids: {len(document_ids or [])} docs")
        if document_ids:
            print(f"  ids (first 5): {document_ids[:5]}")
        print()

    # 3. _fetch_paragraphs (same as bm25_search)
    print("-" * 50)
    print("3. BM25 CORPUS FETCH (_fetch_paragraphs)")
    print("-" * 50)
    from mobius_retriever.bm25_search import _fetch_paragraphs

    tag_filters = {"document_payer": payer} if payer else None
    emitted: list[str] = []

    def emit(m: str) -> None:
        if m.strip():
            emitted.append(m.strip())

    rows = _fetch_paragraphs(
        db_url,
        authority_level=None,
        source_types=None,
        tag_filters=tag_filters,
        document_ids=document_ids,
        emitter=emit,
    )
    print(f"  Paragraphs fetched: {len(rows)}")
    for line in emitted:
        print(f"  {line}")
    if not rows:
        print("  >>> Corpus empty. BM25 will return 0. Check document_payer / tag_filters.")
        print("=" * 70)
        return 0
    print()

    # 4. bm25_search (raw, before cutoff)
    print("-" * 50)
    print("4. BM25 SEARCH (raw, before cutoff)")
    print("-" * 50)
    from mobius_retriever.bm25_search import bm25_search

    emitted2: list[str] = []

    def emit2(m: str) -> None:
        if m.strip():
            emitted2.append(m.strip())

    raw_chunks = bm25_search(
        question=question,
        postgres_url=db_url,
        authority_level=None,
        source_types=None,
        tag_filters=tag_filters,
        document_ids=document_ids,
        top_k=10,
        include_paragraphs=True,
        top_k_per_type=10,
        emitter=emit2,
    )
    for line in emitted2:
        print(f"  {line}")
    print(f"  Raw BM25 chunks: {len(raw_chunks)}")
    if not raw_chunks:
        print("  >>> No BM25 matches. Query tokens may not overlap corpus.")
        print("=" * 70)
        return 0
    print()

    # 5. Cutoff (retrieve_bm25 applies this)
    print("-" * 50)
    print("5. CUTOFF (abstention filter in retrieve_bm25)")
    print("-" * 50)
    from mobius_retriever.config import apply_normalize_bm25, load_bm25_sigmoid_config, load_retrieval_cutoffs

    cutoffs = load_retrieval_cutoffs()
    bm25_cfg = load_bm25_sigmoid_config()
    cutoff_norm = cutoffs.bm25_abstention_cutoff_normalized
    print(f"  cutoff_normalized: >= {cutoff_norm}")

    passed = 0
    for c in raw_chunks:
        raw = c.get("raw_score")
        if raw is None:
            continue
        pt = c.get("provision_type", "sentence")
        norm = apply_normalize_bm25(float(raw), pt, bm25_cfg) if bm25_cfg else min(1.0, float(raw) / 50.0)
        if norm >= cutoff_norm:
            passed += 1

    print(f"  Chunks passing cutoff: {passed} / {len(raw_chunks)}")
    if passed == 0:
        print("  >>> All dropped by cutoff. Check bm25_sigmoid.yaml (x0, k) and retrieval_cutoffs.yaml.")
    print()
    print("=" * 70)
    print("NOTE: This runs INLINE (CHAT_RAG_DATABASE_URL). If RAG API returns bm25_raw_n=0")
    print("      but this script shows BM25 works, the RAG API process may have different")
    print("      env (e.g. CHAT_RAG_DATABASE_URL empty). Restart RAG API with same .env.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
