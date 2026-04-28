"""Phase 2 one-time migration: copy chat_answer_cache rows from
Chroma → pgvector.

Idempotent — replayable until diff is zero. Keys on
``correlation_id`` (carried in Chroma metadata) so re-runs don't
duplicate.

Usage:

    BACKEND=chroma  CHROMA_HOST=...  CHROMA_AUTH_TOKEN=...
    CACHE_DATABASE_URL=postgresql+asyncpg://.../mobius_cache  \\
    python scripts/migrate_chroma_to_pgvector.py [--dry-run] [--limit N]

The script reads from Chroma in batches (Chroma's `.get(limit=...)`
paginates by offset), constructs an asyncpg connection to the
pgvector DB, and bulk-INSERTs with ``ON CONFLICT DO UPDATE``.

Validation pass: after migration, run with ``--validate`` to compare
row counts + spot-check 50 random rows for content equality.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate")


# NOTE: the new agent should fill these in. The skeleton below shows
# the shape; the actual asyncpg calls + the Chroma scan loop are
# straightforward.


async def migrate(*, dry_run: bool, limit: int | None) -> None:
    """Read from Chroma, write to pgvector.

    Pseudocode (TODO for the new agent):

        chroma_coll = get_chroma_collection()
        pg_pool = await asyncpg.create_pool(CACHE_DATABASE_URL)

        offset = 0
        batch_size = 200
        n_seen, n_inserted, n_updated, n_skipped = 0, 0, 0, 0
        while True:
            rows = chroma_coll.get(
                include=["metadatas", "documents", "embeddings"],
                limit=batch_size, offset=offset,
            )
            if not rows.get("ids"):
                break
            for i, cid in enumerate(rows["ids"]):
                meta = rows["metadatas"][i] or {}
                emb = rows["embeddings"][i]
                doc = rows["documents"][i] or ""
                # Reconstruct the row from metadata. Some fields may
                # not have been written (older entries) — default to
                # safe values.
                correlation_id = meta.get("correlation_id") or cid
                if dry_run:
                    n_seen += 1
                    continue
                await pg_pool.execute(\"\"\"
                    INSERT INTO chat_answer_cache
                      (correlation_id, thread_id, question, question_norm,
                       embedding, answer, skill_envelope, config_sha,
                       payer, state, program, authority_level, domain_tags,
                       qc_passed, thumbs_down, caller, answered_at)
                    VALUES ($1, $2, $3, $4, $5::vector, $6, $7::jsonb, $8,
                            $9, $10, $11, $12, $13, $14, $15, $16, $17)
                    ON CONFLICT (correlation_id) DO UPDATE
                    SET answer = EXCLUDED.answer,
                        skill_envelope = EXCLUDED.skill_envelope,
                        answered_at = EXCLUDED.answered_at
                \"\"\", ...)
                n_inserted += 1
            offset += batch_size
            if limit and n_seen >= limit:
                break

        logger.info("done: seen=%d inserted=%d updated=%d skipped=%d",
                    n_seen, n_inserted, n_updated, n_skipped)
    """
    raise NotImplementedError(
        "migrate_chroma_to_pgvector — Phase 2 stub. "
        "Implement after pgvector backend (app/backends/pgvector.py) "
        "is filled in. See pseudocode in this function's docstring."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Count rows; don't write")
    ap.add_argument("--limit", type=int, default=None, help="Cap rows processed (testing)")
    ap.add_argument("--validate", action="store_true", help="Post-migration validation pass")
    args = ap.parse_args()

    asyncio.run(migrate(dry_run=args.dry_run, limit=args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
