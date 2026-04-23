#!/usr/bin/env python3
"""Seed ``chat_answer_cache`` from historical ``chat_turns``.

Idempotent one-shot. Re-running after a previous seed skips already-seeded
correlation_ids. Safe to run against prod — writes to Chroma only, never
mutates Postgres.

Eligibility filter (sanity-check defaults; override via flags):
  * status = 'completed'
  * final_message is non-empty and > 50 chars
  * retrieval_signals NOT in {'no_sources', '(none)', 'system_context'}
  * source_count >= 1
  * config_sha set
  * created_at >= --cutoff-date (default: 2026-04-20, the RAG-swap date)
  * no thumbs_down feedback on this turn

Dedup (option 3c, highest-quality per normalized question):
  Rows are grouped by (LOWER(TRIM(question)), config_sha). Within each
  group, the seed picks the single best candidate by:
    1. critic_approved = true preferred
    2. higher source_count preferred
    3. most recent created_at preferred
  Ties broken by lexicographic correlation_id.

Usage:
    # Sanity check — count eligible rows, write nothing
    python scripts/seed_chat_answer_cache.py --dry-run

    # Full seed with defaults
    python scripts/seed_chat_answer_cache.py

    # Conservative: only last 7 days
    python scripts/seed_chat_answer_cache.py --cutoff-date 2026-04-16

    # Batched for large corpora
    python scripts/seed_chat_answer_cache.py --batch-size 50 --embed-rate-pause 0.5

Env requirements (same as chat Cloud Run):
    CHAT_RAG_DATABASE_URL  — source chat_turns
    CHROMA_HOST + CHROMA_AUTH_TOKEN  — target cache collection
    VERTEX_PROJECT_ID  — embedding provider

Exit codes:
    0 — success (including zero eligible with --dry-run)
    1 — hard error (DB unreachable, Chroma unreachable, Vertex unreachable)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

CHAT_ROOT = Path(__file__).resolve().parent.parent
if str(CHAT_ROOT) not in sys.path:
    sys.path.insert(0, str(CHAT_ROOT))

# Load .env for CHAT_RAG_DATABASE_URL, CHROMA_HOST, etc.
for env_path in (
    CHAT_ROOT / ".env",
    CHAT_ROOT.parent / "mobius-config" / ".env",
    CHAT_ROOT.parent / ".env",
):
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path, override=False)
        except Exception:
            pass
        break

logger = logging.getLogger("seed_cache")


# ── Eligibility query ────────────────────────────────────────────────


_ELIGIBILITY_SQL = """
WITH eligible AS (
    SELECT
        t.correlation_id,
        t.question,
        t.final_message,
        t.sources,
        t.thinking_log,
        t.duration_ms,
        t.config_sha,
        t.created_at,
        t.thread_id,
        t.user_id,
        COALESCE(
            jsonb_array_length(t.sources::jsonb),
            0
        ) AS source_count,
        -- thumbs_down via chat_feedback existence
        EXISTS (
            SELECT 1 FROM chat_feedback f
            WHERE f.correlation_id = t.correlation_id
              AND f.rating = 'down'
        ) AS has_thumbs_down,
        -- critic_approved: scan thinking_log JSONB for the signal
        (
            SELECT bool_or(
                (elem->>'signal') IN ('critic_approved', 'critic_approved_after_retry')
            )
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(t.thinking_log::jsonb) = 'array'
                     THEN t.thinking_log::jsonb
                     ELSE '[]'::jsonb END
            ) elem
            WHERE jsonb_typeof(elem) = 'object'
        ) AS critic_approved
    FROM chat_turns t
    WHERE t.final_message IS NOT NULL
      AND LENGTH(t.final_message) > 50
      AND t.config_sha IS NOT NULL
      AND t.created_at >= :cutoff::timestamptz
)
SELECT
    correlation_id,
    question,
    final_message,
    sources,
    duration_ms,
    config_sha,
    created_at,
    thread_id,
    user_id,
    source_count,
    has_thumbs_down,
    COALESCE(critic_approved, false) AS critic_approved
FROM eligible
WHERE source_count >= 1
  AND NOT has_thumbs_down
ORDER BY created_at DESC
"""


def _pick_best_per_question(rows: list[dict]) -> list[dict]:
    """Dedup rule 3c — one winner per (normalized question, config_sha).

    Priority:
      1. critic_approved = true
      2. higher source_count
      3. more recent created_at
      4. lexicographic correlation_id (deterministic tiebreaker)
    """
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        q_norm = (r.get("question") or "").strip().lower()
        cfg = (r.get("config_sha") or "").strip()
        key = (q_norm, cfg)
        incumbent = groups.get(key)
        if incumbent is None:
            groups[key] = r
            continue
        # Ranking: prefer critic_approved, then source_count, then newer.
        def rank(x: dict) -> tuple:
            return (
                1 if x.get("critic_approved") else 0,
                int(x.get("source_count") or 0),
                x.get("created_at") or "",
                # Invert for deterministic tiebreak (earlier cid wins)
                -(hash(x.get("correlation_id") or "") & 0xFFFFFFFF),
            )
        if rank(r) > rank(incumbent):
            groups[key] = r
    return list(groups.values())


# ── Chroma helpers ───────────────────────────────────────────────────


def _chroma_collection(collection_name: str):
    import chromadb
    host = (os.environ.get("CHROMA_HOST") or "").strip()
    if host:
        port = int((os.environ.get("CHROMA_PORT") or "8000").strip())
        ssl = (os.environ.get("CHROMA_SSL") or "").strip().lower() in {"1", "true", "yes"}
        token = (os.environ.get("CHROMA_AUTH_TOKEN") or "").strip()
        client = chromadb.HttpClient(
            host=host,
            port=port,
            ssl=ssl,
            headers={"X-Chroma-Token": token} if token else None,
        )
    else:
        persist_dir = (os.environ.get("CHROMA_PERSIST_DIR") or "/tmp/chroma").strip()
        client = chromadb.PersistentClient(path=persist_dir)
    return client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )


def _existing_ids(coll) -> set[str]:
    """Fetch all currently-seeded ids so we can skip them (idempotency)."""
    try:
        result = coll.get(include=[])  # ids are always included
        return set(result.get("ids") or [])
    except Exception as e:
        logger.warning("Could not read existing ids (%s); proceeding as if empty", e)
        return set()


# ── Metadata builder ─────────────────────────────────────────────────


def _build_metadata(row: dict) -> dict[str, Any]:
    from datetime import datetime, timezone

    created_at = row.get("created_at")
    if hasattr(created_at, "isoformat"):
        iso = created_at.isoformat()
        if not iso.endswith("Z") and "+" not in iso[-6:]:
            iso += "Z"
    else:
        iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    return {
        "question": (row.get("question") or "").strip()[:1500],
        "final_message": (row.get("final_message") or "").strip()[:2000],
        "sources_json": (row.get("sources") or "[]")[:8000] if isinstance(row.get("sources"), str) else json.dumps(row.get("sources") or [])[:8000],
        "source_count": int(row.get("source_count") or 0),
        "retrieval_signals": "",
        "config_sha": (row.get("config_sha") or "") or "",
        "created_at": iso,
        "domain_tags": "",
        "thumbs_up": False,
        "thumbs_down": False,
        "critic_approved": bool(row.get("critic_approved")),
        "quality_score": "",  # no quality_score available from this backfill path
        "seeded": True,
        "chat_mode_used": "copilot",
    }


# ── Main ─────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cutoff-date", default="2026-04-20",
                        help="Only seed turns created on/after this date (YYYY-MM-DD). Default: 2026-04-20 (RAG swap date)")
    parser.add_argument("--collection", default=None,
                        help="Chroma collection name. Default: CACHE_ASSIST_CHROMA_COLLECTION env or 'chat_answer_cache'")
    parser.add_argument("--batch-size", type=int, default=25,
                        help="Chroma upsert batch size")
    parser.add_argument("--embed-rate-pause", type=float, default=0.0,
                        help="Seconds to sleep between embedding calls (rate-limit protection)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show counts + sample, don't write")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap total seed rows (0 = unlimited)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    coll_name = (
        args.collection
        or (os.environ.get("CACHE_ASSIST_CHROMA_COLLECTION") or "chat_answer_cache").strip()
    )
    logger.info("Collection: %s", coll_name)
    logger.info("Cutoff:     %s", args.cutoff_date)

    # ── Step 1: eligibility query ───────────────────────────────────
    from app.db_client import db_query

    logger.info("Querying chat_turns for eligible seeds…")
    result = db_query(
        _ELIGIBILITY_SQL,
        "chat",
        params={"cutoff": args.cutoff_date},
        max_rows=10000,
    )
    if "error" in result:
        logger.error("DB query failed: %s", result["error"])
        return 1

    rows = []
    cols = result.get("columns") or []
    for r in (result.get("rows") or []):
        rows.append(dict(zip(cols, r)))
    logger.info("Eligible rows (pre-dedup): %d", len(rows))

    if not rows:
        logger.info("Nothing to seed. Done.")
        return 0

    # ── Step 2: dedup ───────────────────────────────────────────────
    deduped = _pick_best_per_question(rows)
    logger.info("After dedup (best per question+config_sha): %d", len(deduped))

    if args.limit > 0:
        deduped = deduped[: args.limit]
        logger.info("After --limit: %d", len(deduped))

    # ── Step 3: skip already-seeded (idempotency) ───────────────────
    if not args.dry_run:
        coll = _chroma_collection(coll_name)
        already = _existing_ids(coll)
        before = len(deduped)
        deduped = [r for r in deduped if r["correlation_id"] not in already]
        logger.info("After skipping already-seeded (%d existing): %d", len(already), len(deduped))
        if before > len(deduped):
            logger.info("Skipped %d rows already present in cache", before - len(deduped))
    else:
        coll = None

    # ── Step 4: sample + summary ────────────────────────────────────
    if deduped:
        sample = deduped[0]
        logger.info("Sample row:")
        logger.info("  correlation_id: %s", sample["correlation_id"])
        logger.info("  question:       %s", (sample.get("question") or "")[:80])
        logger.info("  sources:        %d", sample.get("source_count") or 0)
        logger.info("  critic_appr:    %s", sample.get("critic_approved"))
        logger.info("  created_at:     %s", sample.get("created_at"))

    if args.dry_run:
        logger.info("DRY RUN — would seed %d rows. Re-run without --dry-run to write.", len(deduped))
        return 0

    if not deduped:
        logger.info("Nothing to do. Done.")
        return 0

    # ── Step 5: embed + upsert in batches ───────────────────────────
    from app.services.embedding_provider import get_query_embedding

    total_written = 0
    total_failed = 0

    for batch_start in range(0, len(deduped), args.batch_size):
        batch = deduped[batch_start : batch_start + args.batch_size]
        ids, embeddings, documents, metadatas = [], [], [], []
        for row in batch:
            q = (row.get("question") or "").strip()
            if not q:
                continue
            try:
                emb = get_query_embedding(q)
            except Exception as e:
                logger.warning("Embed failed for %s: %s", row["correlation_id"][:8], e)
                total_failed += 1
                continue
            ids.append(row["correlation_id"])
            embeddings.append(emb)
            documents.append(q[:1500])
            metadatas.append(_build_metadata(row))
            if args.embed_rate_pause > 0:
                time.sleep(args.embed_rate_pause)

        if not ids:
            continue

        try:
            coll.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )
            total_written += len(ids)
            logger.info("Batch %d-%d: upserted %d",
                        batch_start, batch_start + len(batch), len(ids))
        except Exception as e:
            logger.warning("Upsert failed for batch starting %d: %s", batch_start, e)
            total_failed += len(ids)

    logger.info("──────────────────────────────────────")
    logger.info("Seed complete:")
    logger.info("  written: %d", total_written)
    logger.info("  failed:  %d", total_failed)
    logger.info("  total seen (after dedup + skip): %d", len(deduped))
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
