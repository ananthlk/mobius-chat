"""mobius-answer-cache — FastAPI service skeleton.

Two skill endpoints + a small admin surface for history queries.
Backend (Chroma in Phase 0, pgvector in Phase 1+) is selected by
the ``BACKEND`` env var and resolved through ``app.backends.get_backend()``.

Routes:

  POST /api/skills/v1/cache_lookup
  POST /api/skills/v1/cache_write
  PATCH /api/skills/v1/cache_thumbs_down
  DELETE /api/skills/v1/cache_invalidate

  GET  /admin/history
  GET  /admin/cache_stats

  GET  /health/deep
  GET  /health
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.backends import get_backend
from app.embedding import embed_question
from app.skills.cache_lookup import lookup_handler
from app.skills.cache_write import write_handler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="mobius-answer-cache", version="0.1.0")


# ── Request / response models ────────────────────────────────────────


class CacheLookupRequest(BaseModel):
    question: str
    config_sha: Optional[str] = None
    filters: dict[str, Any] = Field(default_factory=dict)
    min_similarity: Optional[float] = 0.85
    k: int = 5
    caller: str = "unknown"


class CacheLookupCandidate(BaseModel):
    candidate_id: str
    question: str
    answer: str
    skill_envelope: dict[str, Any]
    similarity: float
    age_days: Optional[float] = None
    config_sha: Optional[str] = None
    thumbs_down: bool = False
    domain_tags: list[str] = Field(default_factory=list)
    thread_id: Optional[str] = None
    answered_at: Optional[str] = None


class CacheLookupResponse(BaseModel):
    candidates: list[CacheLookupCandidate]
    telemetry: dict[str, Any]


class CacheWriteRequest(BaseModel):
    correlation_id: str
    thread_id: Optional[str] = None
    question: str
    answer: str
    skill_envelope: dict[str, Any] = Field(default_factory=dict)
    config_sha: Optional[str] = None
    filters: dict[str, Any] = Field(default_factory=dict)
    domain_tags: list[str] = Field(default_factory=list)
    qc_passed: bool = True
    thumbs_down: bool = False
    caller: str = "unknown"


class CacheWriteResponse(BaseModel):
    candidate_id: str
    embed_ms: int
    write_ms: int


class CacheThumbsDownRequest(BaseModel):
    candidate_id: str
    reason: Optional[str] = None


class CacheInvalidateRequest(BaseModel):
    filter: dict[str, Any] = Field(default_factory=dict)


# ── Routes ───────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/deep")
def health_deep() -> dict[str, Any]:
    """Deep health — checks backend reachability."""
    backend = get_backend()
    try:
        ok = backend.health_check()
    except Exception as e:
        return {"status": "degraded", "backend": backend.name, "error": str(e)[:200]}
    return {"status": "ok" if ok else "degraded", "backend": backend.name}


@app.post("/api/skills/v1/cache_lookup", response_model=CacheLookupResponse)
def cache_lookup(request: Request, body: CacheLookupRequest = Body(...)) -> CacheLookupResponse:
    caller = (request.headers.get("X-Caller") or body.caller or "unknown").strip()
    caller_id = (request.headers.get("X-Caller-Id") or "").strip() or None
    return lookup_handler(
        question=body.question,
        config_sha=body.config_sha,
        filters=body.filters or {},
        min_similarity=body.min_similarity if body.min_similarity is not None else 0.85,
        k=max(1, min(50, body.k)),
        caller=caller,
        caller_id=caller_id,
    )


@app.post("/api/skills/v1/cache_write", response_model=CacheWriteResponse)
def cache_write(request: Request, body: CacheWriteRequest = Body(...)) -> CacheWriteResponse:
    caller = (request.headers.get("X-Caller") or body.caller or "unknown").strip()
    return write_handler(
        correlation_id=body.correlation_id,
        thread_id=body.thread_id,
        question=body.question,
        answer=body.answer,
        skill_envelope=body.skill_envelope or {},
        config_sha=body.config_sha,
        filters=body.filters or {},
        domain_tags=list(body.domain_tags or []),
        qc_passed=bool(body.qc_passed),
        thumbs_down=bool(body.thumbs_down),
        caller=caller,
    )


@app.patch("/api/skills/v1/cache_thumbs_down", status_code=204)
def cache_thumbs_down(body: CacheThumbsDownRequest = Body(...)) -> None:
    backend = get_backend()
    backend.mark_thumbs_down(body.candidate_id, reason=body.reason)
    return None


@app.delete("/api/skills/v1/cache_invalidate")
def cache_invalidate(body: CacheInvalidateRequest = Body(...)) -> dict[str, int]:
    backend = get_backend()
    n = backend.bulk_invalidate(filter=body.filter or {})
    return {"invalidated_count": n}


@app.get("/admin/history")
def admin_history(
    thread_id: Optional[str] = Query(None),
    caller: Optional[str] = Query(None),
    since: str = Query("24h"),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    backend = get_backend()
    rows = backend.list_history(
        thread_id=thread_id, caller=caller, since=since, limit=limit,
    )
    return {"count": len(rows), "rows": rows}


@app.get("/admin/cache_stats")
def admin_cache_stats(since: str = Query("24h")) -> dict[str, Any]:
    backend = get_backend()
    return backend.stats(since=since)
