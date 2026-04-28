"""cache_lookup skill handler — server-side.

Maps the HTTP request body into the backend's lookup() call,
embeds the question, returns the candidate list + telemetry.

Source: derived from mobius-chat's
``app/skills/builtin/cached_answer.py`` (handler + filter parsing).
The chat side becomes a thin HTTP client; the actual logic lives
here so the storage backend can swap underneath without chat
changes.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from app.backends import get_backend
from app.embedding import embed_question

logger = logging.getLogger(__name__)


def lookup_handler(
    *,
    question: str,
    config_sha: str | None,
    filters: dict[str, Any],
    min_similarity: float,
    k: int,
    caller: str,
    caller_id: str | None,
) -> "CacheLookupResponse":  # type: ignore[name-defined]  -- forward ref to main.py model
    """Run a cache_lookup. Returns a response model from main.py.

    The wrapping is done in main.py so this module stays free of
    pydantic — easier to test in isolation.
    """
    from app.main import CacheLookupCandidate, CacheLookupResponse

    lookup_id = str(uuid.uuid4())
    t0 = time.perf_counter()

    if not (question or "").strip():
        return CacheLookupResponse(
            candidates=[],
            telemetry={
                "lookup_id": lookup_id,
                "embed_ms": 0,
                "ann_ms": 0,
                "filter_ms": 0,
                "total_ms": 0,
                "n_in_pool": 0,
                "error": "empty_query",
                "caller": caller,
                "caller_id": caller_id,
            },
        )

    # Embed query
    t_embed = time.perf_counter()
    try:
        embedding = embed_question(question)
    except Exception as e:
        logger.warning("cache_lookup: embed failed: %s", e)
        return CacheLookupResponse(
            candidates=[],
            telemetry={
                "lookup_id": lookup_id,
                "embed_ms": int((time.perf_counter() - t_embed) * 1000),
                "total_ms": int((time.perf_counter() - t0) * 1000),
                "error": f"embed_failed: {e}",
                "caller": caller,
                "caller_id": caller_id,
            },
        )
    embed_ms = int((time.perf_counter() - t_embed) * 1000)

    # Backend lookup
    backend = get_backend()
    t_ann = time.perf_counter()
    try:
        rows = backend.lookup(
            embedding=embedding,
            config_sha=config_sha,
            filters=filters or {},
            min_similarity=min_similarity,
            k=k,
        )
    except Exception as e:
        logger.warning("cache_lookup: backend %s lookup failed: %s", backend.name, e)
        return CacheLookupResponse(
            candidates=[],
            telemetry={
                "lookup_id": lookup_id,
                "embed_ms": embed_ms,
                "total_ms": int((time.perf_counter() - t0) * 1000),
                "error": f"backend_failed: {e}",
                "backend": backend.name,
                "caller": caller,
                "caller_id": caller_id,
            },
        )
    ann_ms = int((time.perf_counter() - t_ann) * 1000)

    candidates = [
        CacheLookupCandidate(
            candidate_id=str(r.get("candidate_id") or ""),
            question=str(r.get("question") or ""),
            answer=str(r.get("answer") or ""),
            skill_envelope=r.get("skill_envelope") or {},
            similarity=float(r.get("similarity") or 0.0),
            age_days=r.get("age_days"),
            config_sha=r.get("config_sha"),
            thumbs_down=bool(r.get("thumbs_down")),
            domain_tags=list(r.get("domain_tags") or []),
            thread_id=r.get("thread_id"),
            answered_at=r.get("answered_at"),
        )
        for r in rows
    ]

    return CacheLookupResponse(
        candidates=candidates,
        telemetry={
            "lookup_id": lookup_id,
            "backend": backend.name,
            "embed_ms": embed_ms,
            "ann_ms": ann_ms,
            "total_ms": int((time.perf_counter() - t0) * 1000),
            "n_in_pool": len(candidates),
            "min_similarity": min_similarity,
            "k": k,
            "caller": caller,
            "caller_id": caller_id,
        },
    )
