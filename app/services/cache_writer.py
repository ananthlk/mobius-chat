"""Cache writer — daemon thread that populates ``chat_answer_cache`` after
successful turns.

Fire-and-forget from ``_publish_completed``, same pattern as
``post_run_adjudication``. Runs on a background thread so persistence
latency never affects the user-visible turn time.

Write-gate policy (who gets cached):
    * status == 'completed'
    * final_message is non-empty (> 30 chars)
    * retrieval_signals is NOT in {'no_sources', ''} — don't cache
      "I couldn't find an answer" turns
    * source_count >= 1 — if the answer has no sources, don't promote
      it as a reusable cached answer (rare edge: system_context
      short-circuits have 0 sources but still produce real answers;
      those are explicitly excluded below by retrieval_signal check —
      'system_context' signal is NOT a cacheable answer because the
      caller supplied the ground truth, not us)
    * config_sha is set (we need it for cache invalidation)
    * NOT cache-sourced itself (would create a feedback loop where
      a cached answer becomes its own evidence); checked via the
      ``cache_influence`` field we stamp on the turn

The write:
    * Embed the question using the existing embedding provider
      (1536-dim, gemini-embedding-001 — SAME model the reader uses)
    * Upsert into the chat_answer_cache Chroma collection with a
      metadata payload capturing the answer, sources (as JSON),
      quality signals, config_sha, domain_tags (if derivable from
      active payer/state), and created_at timestamp

Env gates:
    * ``CACHE_ASSIST_ENABLED`` — master switch; when off, writer
      never fires regardless of per-turn config
    * ``CACHE_ASSIST_WRITE_QUALITY_FLOOR`` — if set and the turn has
      an adjudicator quality_score, only cache when >= floor

Failure mode: swallowed. Cache is derived data; a failed write
should never break a chat turn. Loud log, move on. A later re-seed
run can backfill anything that missed.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ── Write-gate ────────────────────────────────────────────────────────


def _enabled() -> bool:
    raw = (os.environ.get("CACHE_ASSIST_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _quality_floor() -> float:
    try:
        return float(os.environ.get("CACHE_ASSIST_WRITE_QUALITY_FLOOR") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# Retrieval signals that DO NOT produce a cacheable answer. ``system_context``
# is caller-authored ground truth — re-caching it would create a feedback
# loop where the cache claims authorship of text it didn't generate. The
# caller should re-supply system_context on each invocation that needs it.
_NON_CACHEABLE_SIGNALS = frozenset({
    "no_sources",
    "",
    "system_context",
})


def _should_cache(ctx, payload: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether this turn's answer should be cached. Returns
    (should_cache, reason) — reason is diagnostic when False."""
    if not _enabled():
        return False, "cache_assist_disabled"

    status = (payload or {}).get("status")
    if status != "completed":
        return False, f"status={status!r}"

    msg = (payload or {}).get("message") or (payload or {}).get("final_message") or ""
    if not isinstance(msg, str) or len(msg.strip()) < 30:
        return False, "final_message_too_short"

    # Retrieval signal check: union of orchestrator-provided list.
    signals_raw = (payload or {}).get("retrieval_signals") or []
    signals = {str(s).strip().lower() for s in signals_raw if s is not None}
    if not signals or all(s in _NON_CACHEABLE_SIGNALS for s in signals):
        return False, "retrieval_signal_not_cacheable"

    sources = (payload or {}).get("sources") or []
    if not isinstance(sources, list) or len(sources) < 1:
        return False, "source_count_zero"

    cache_influence = getattr(ctx, "cache_influence", None) or ""
    # If this turn itself used cache verbatim, re-caching it would
    # propagate the existing cache entry under a new id. Skip.
    if cache_influence in ("verbatim",):
        return False, "turn_used_cache_verbatim"

    floor = _quality_floor()
    if floor > 0:
        q = None
        # quality_score lives in a few possible places depending on
        # pipeline version; check all defensively.
        for src in (payload.get("qc_audit"), payload.get("technical_feedback")):
            if isinstance(src, dict):
                qs = src.get("quality_score") or src.get("score")
                try:
                    q = float(qs) if qs is not None else None
                    break
                except (TypeError, ValueError):
                    pass
        if q is not None and q < floor:
            return False, f"quality_score={q:.2f}<floor={floor:.2f}"

    return True, "ok"


# ── Metadata builder ──────────────────────────────────────────────────


def _domain_tags_from_ctx(ctx) -> list[str]:
    """Derive domain tags from active payer/state on the context.

    Intentionally conservative: only emit tags we're confident about.
    Callers downstream can filter on these, but the cache itself
    shouldn't invent tags. Future: a real domain-classifier step
    that tags by question type (policy / strategy / operational)."""
    tags: list[str] = []
    active = (getattr(ctx, "merged_state", None) or {}).get("active") or {}
    if isinstance(active, dict):
        payer = (active.get("payer") or "").strip()
        state = (active.get("state") or "").strip()
        if payer:
            tags.append(f"payer:{payer.lower().replace(' ', '_')}")
        if state:
            tags.append(f"state:{state.lower()}")
    return tags


def _build_metadata(ctx, payload: dict[str, Any]) -> dict[str, Any]:
    msg = (payload.get("message") or payload.get("final_message") or "").strip()
    sources = payload.get("sources") or []
    signals = payload.get("retrieval_signals") or []

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    # Quality signals — all best-effort, never block the write.
    thumbs_up = False
    thumbs_down = False
    critic_approved = False
    quality_score: float | None = None
    tl = payload.get("thinking_log") or []
    for entry in tl:
        if isinstance(entry, dict):
            sig = entry.get("signal") or ""
            if sig == "critic_approved":
                critic_approved = True
            elif sig in ("critic_approved_after_retry",):
                critic_approved = True

    # Metadata must be JSON-primitives per Chroma; flatten everything.
    return {
        "question": (ctx.refined_query or ctx.message or "").strip()[:1500],
        "final_message": msg[:2000],
        "sources_json": json.dumps(sources)[:8000],
        "source_count": int(len(sources)),
        "retrieval_signals": ",".join(str(s) for s in signals if s)[:500],
        "config_sha": (payload.get("config_sha") or "") or "",
        "created_at": now_iso,
        "domain_tags": ",".join(_domain_tags_from_ctx(ctx)),
        "thumbs_up": thumbs_up,
        "thumbs_down": thumbs_down,
        "critic_approved": critic_approved,
        "quality_score": float(quality_score) if quality_score is not None else None,
        "seeded": False,
        "chat_mode_used": (getattr(ctx, "chat_mode", None) or "copilot"),
    }


# ── Entry point ───────────────────────────────────────────────────────


def _thread_main(correlation_id: str, question: str, metadata: dict[str, Any]) -> None:
    """Background thread: embed + upsert. Never raises back to caller."""
    try:
        from app.services.embedding_provider import get_query_embedding
        from app.skills.builtin.cached_answer import _get_cache_collection

        embedding = get_query_embedding(question)
        coll = _get_cache_collection()

        # Chroma's metadata can't carry None values — convert None → sentinel.
        clean_meta = {k: (v if v is not None else "") for k, v in metadata.items()}

        coll.upsert(
            ids=[correlation_id],
            embeddings=[embedding],
            documents=[question[:1500]],
            metadatas=[clean_meta],
        )
        logger.info("cache writer: wrote cid=%s", correlation_id[:8])
    except Exception as e:
        logger.warning("cache writer failed for cid=%s: %s", correlation_id[:8], e)


def schedule_cache_write(ctx, payload: dict[str, Any]) -> None:
    """Fire-and-forget. Call from ``_publish_completed`` AFTER the
    response has been queued + persisted.

    Runs on a daemon thread so the chat turn's user-visible latency
    is unaffected. The only work on the synchronous path is the
    write-gate check + thread start (< 1ms).
    """
    try:
        should, reason = _should_cache(ctx, payload)
    except Exception as e:
        logger.debug("cache writer gate raised: %s", e)
        return

    if not should:
        logger.debug(
            "cache writer skip cid=%s reason=%s",
            getattr(ctx, "correlation_id", "")[:8],
            reason,
        )
        return

    try:
        question = (ctx.refined_query or ctx.message or "").strip()
        metadata = _build_metadata(ctx, payload)
        t = threading.Thread(
            target=_thread_main,
            args=(ctx.correlation_id, question, metadata),
            daemon=True,
            name=f"cache-write-{ctx.correlation_id[:8]}",
        )
        t.start()
    except Exception as e:
        logger.warning("cache writer schedule failed: %s", e)
