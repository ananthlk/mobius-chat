"""Cache backend interface — what every backend implementation must provide.

Backends are NOT generic vector stores. The cache has a specific shape:

  * Each row is a (question, answer, skill_envelope, …) triple keyed
    by ``correlation_id`` (chat turn cid). Replays with the same cid
    are no-ops, not duplicates.
  * Lookup is semantic-similarity by question embedding, with rich
    filters (max_age, payer/state/program, thumbs_down, config_sha,
    domain_tags).
  * History queries are time + thread + caller scoped — these need
    to work efficiently on the backend (pgvector with btree indexes
    is good; Chroma's metadata filters are awkward).

Phase 0 (Chroma) maps embed → vector search → metadata filter; the
history surface is best-effort (paginated metadata scan).

Phase 1+ (pgvector) is the natural fit and where the API gets fast
+ rich.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class CacheBackend(ABC):
    """All cache backends implement this interface. The HTTP layer in
    main.py only ever talks to the interface; concrete backends never
    leak into request/response shape."""

    name: str = "abstract"

    # ── Lookup ────────────────────────────────────────────────────────

    @abstractmethod
    def lookup(
        self,
        *,
        embedding: list[float],
        config_sha: str | None,
        filters: dict[str, Any],
        min_similarity: float,
        k: int,
    ) -> list[dict[str, Any]]:
        """Return up to k candidate rows ordered by similarity desc.

        Each row dict carries: candidate_id, question, answer,
        skill_envelope, similarity, age_days, config_sha, thumbs_down,
        domain_tags, thread_id, answered_at.

        The HTTP layer wraps these into the response model — backends
        return dicts to keep the contract loose.
        """
        ...

    # ── Write ─────────────────────────────────────────────────────────

    @abstractmethod
    def write(
        self,
        *,
        correlation_id: str,
        embedding: list[float],
        thread_id: str | None,
        question: str,
        answer: str,
        skill_envelope: dict[str, Any],
        config_sha: str | None,
        filters: dict[str, Any],
        domain_tags: list[str],
        qc_passed: bool,
        thumbs_down: bool,
        caller: str,
    ) -> str:
        """Idempotent on ``correlation_id`` — second write returns the
        same candidate_id without inserting again.

        Returns the candidate_id (uuid string).
        """
        ...

    # ── Mutations ─────────────────────────────────────────────────────

    @abstractmethod
    def mark_thumbs_down(self, candidate_id: str, *, reason: str | None = None) -> None:
        """Soft-delete a row from future lookups by setting
        thumbs_down=True. The row remains in the table for history."""
        ...

    @abstractmethod
    def bulk_invalidate(self, *, filter: dict[str, Any]) -> int:
        """Hard-delete rows matching the filter (e.g. all rows where
        config_sha=<old_sha>). Returns delete count.

        Use sparingly — invalidates real history.
        """
        ...

    # ── History / analytics ───────────────────────────────────────────

    @abstractmethod
    def list_history(
        self,
        *,
        thread_id: str | None,
        caller: str | None,
        since: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Time-ordered history of cache rows matching the filters.
        ``since`` is a duration spec — '24h', '7d', etc."""
        ...

    @abstractmethod
    def stats(self, *, since: str) -> dict[str, Any]:
        """Operational counters: writes, hit rate, top repeated
        questions. Best-effort on Chroma (metadata scans); native
        on pgvector."""
        ...

    # ── Health ────────────────────────────────────────────────────────

    @abstractmethod
    def health_check(self) -> bool:
        """True if the backend is reachable and the schema/collection
        exists. Called by /health/deep."""
        ...
