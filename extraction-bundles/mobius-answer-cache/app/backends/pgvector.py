"""pgvector backend (Phase 1+).

STUB — methods raise ``NotImplementedError`` until the new agent
fills them in. The shape is defined; the SQL is the work.

Schema is in ``migrations/001_chat_answer_cache.sql``. Run that
against the target DB before deploying with ``BACKEND=pgvector``.

Implementation notes for the new agent:

  * Use asyncpg for connection pooling — same pattern as mobius-rag
    (mobius-rag/app/database.py is a clean template).
  * Embedding similarity: ``1 - (embedding <=> $1::vector)`` for
    cosine, then filter to ``min_similarity``. HNSW index makes ANN
    fast enough that no rerank is needed at the cache layer.
  * Filter pushdown: payer / state / program / authority_level /
    thumbs_down / max_age — all native btree indexes from the
    schema. Push these into the WHERE so we don't pull rows we
    won't return.
  * Idempotent write: ``ON CONFLICT (correlation_id) DO UPDATE``
    keyed on the unique constraint. Re-embed only on insert path
    if you want to save Vertex calls.
  * History queries: just SQL. ``ORDER BY answered_at DESC LIMIT``
    on the (thread_id, answered_at DESC) index. Cheap.
  * stats(): ``GROUP BY caller``, COUNT(*), filtered by interval.
    Cheap with the (caller, answered_at) index.
"""
from __future__ import annotations

import logging
from typing import Any

from app.backends.base import CacheBackend

logger = logging.getLogger(__name__)


class PgVectorBackend(CacheBackend):
    name = "pgvector"

    def __init__(self) -> None:
        # TODO: build asyncpg pool from CACHE_DATABASE_URL env (or
        # equivalent). See mobius-rag/app/database.py.
        pass

    def lookup(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError("PgVectorBackend.lookup() — Phase 1 stub")

    def write(self, **kwargs: Any) -> str:
        raise NotImplementedError("PgVectorBackend.write() — Phase 1 stub")

    def mark_thumbs_down(self, candidate_id: str, *, reason: str | None = None) -> None:
        raise NotImplementedError("PgVectorBackend.mark_thumbs_down() — Phase 1 stub")

    def bulk_invalidate(self, *, filter: dict[str, Any]) -> int:
        raise NotImplementedError("PgVectorBackend.bulk_invalidate() — Phase 1 stub")

    def list_history(
        self,
        *,
        thread_id: str | None,
        caller: str | None,
        since: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("PgVectorBackend.list_history() — Phase 1 stub")

    def stats(self, *, since: str) -> dict[str, Any]:
        raise NotImplementedError("PgVectorBackend.stats() — Phase 1 stub")

    def health_check(self) -> bool:
        # When implemented: SELECT 1 against the cache table.
        return False
