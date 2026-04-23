"""Builtin skill: ``cached_answer_lookup`` — semantic retrieval of past answers.

Skill-as-neutral-primitive. This handler does NOT decide what a "good"
cached answer is — it retrieves candidates by semantic similarity,
applies the caller-supplied filters, and returns the shortlist. Quality
/ freshness policy lives at the invocation site:

  * Chat's orchestrator invokes this skill with chat-mode-appropriate
    defaults (e.g. ``max_age_days=14`` for copilot) on every turn that
    isn't opted out.
  * Future specialized agents (strategy / policy / analytics) can
    invoke the same skill with their own profiles — a strategy agent
    might pass ``max_age_days=90`` (strategy data is stable); a policy
    agent might pass ``max_age_days=3`` (policy changes frequently).

The caller then reasons over the returned candidates. This skill does
not perform LLM-as-judge comparison; it returns facts and lets the
reasoner decide.

Storage: ChromaDB collection ``chat_answer_cache`` on the same cluster
as ``published_rag``. Reuses the existing ``CHROMA_HOST`` / auth token
wiring — zero new infra. Write path lives in
``app/services/cache_writer.py`` (daemon thread triggered from
``_publish_completed``).

Envelope shape:
    text:    Human-readable summary of top candidates (rendered into
             the reasoning context by the existing tool_results builder).
    sources: One SourceRef per candidate, with the cached answer text,
             original question, date, similarity, and provenance stamps
             in the ``text`` field of the SourceRef.
    signal:  ``cache_hit`` when at least one candidate was returned;
             ``no_sources`` when zero survived filtering.
    extra:   Structured candidate list for programmatic callers.
             {"candidates": [{similarity, age_days, ...}]}

``visible_to_planner=True`` so the tool manifest advertises the skill
and the planner CAN invoke it explicitly (e.g. in round 3 when it
realizes "this looks like a repeat"). The orchestrator also
auto-invokes it pre-round-1 when configured; the two paths are
independent and both benefit from the same handler.

``supports_modes`` excludes agentic — agentic users explicitly opted
into deep analysis; cache-assist defeats their intent. This is enforced
at the registry's manifest-rendering layer, not at dispatch time, so
programmatic callers (agents) can still invoke the skill directly.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from app.skills.registry import (
    SkillCall,
    SkillEnvelope,
    SkillSpec,
    SourceRef,
    register,
)

logger = logging.getLogger(__name__)


# ── Defaults (overridable via SkillCall.inputs per invocation) ─────────

DEFAULT_SIMILARITY_FLOOR = 0.82
DEFAULT_TOP_K = 3
DEFAULT_REQUIRE_NO_THUMBS_DOWN = True
# max_age_days default is None — we want the CALLER to make that choice.
# Chat's orchestrator supplies CACHE_ASSIST_DEFAULT_MAX_AGE_DAYS env value.
# A strategy agent might supply 90; a policy agent 3. No silent default
# here so callers can't accidentally get stale data by forgetting.


# ── Client cache (one Chroma collection per process) ──────────────────

_CACHE_COLLECTION = None


def _get_cache_collection():
    """Lazy-connect to the chat_answer_cache Chroma collection.

    Reuses the existing CHROMA_HOST / CHROMA_AUTH_TOKEN / CHROMA_SSL
    env wiring from ``published_rag_search._get_chroma_collection`` —
    just a different collection name. Connection errors surface to the
    caller so the orchestrator can fall back to "no cache available"
    gracefully.
    """
    global _CACHE_COLLECTION
    if _CACHE_COLLECTION is not None:
        return _CACHE_COLLECTION

    import chromadb

    host = (os.environ.get("CHROMA_HOST") or "").strip()
    collection_name = (
        os.environ.get("CACHE_ASSIST_CHROMA_COLLECTION") or "chat_answer_cache"
    )
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

    _CACHE_COLLECTION = client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )
    logger.info("cache skill: connected to collection %s", collection_name)
    return _CACHE_COLLECTION


def _reset_collection_cache() -> None:
    """Test-only: clear the process-level collection cache so a new
    collection (e.g. an in-memory PersistentClient in tests) can be
    picked up on the next call."""
    global _CACHE_COLLECTION
    _CACHE_COLLECTION = None


# ── Filter plumbing ────────────────────────────────────────────────────


def _parse_inputs(inputs: dict[str, Any] | None) -> dict[str, Any]:
    """Extract + validate filter inputs. Missing values → skill-level
    defaults. Invalid values → skill-level defaults + a warning log
    (never raise; don't let the planner breaking the skill break the
    turn)."""
    inputs = inputs or {}

    def _num(key, default, cast):
        v = inputs.get(key)
        if v is None:
            return default
        try:
            return cast(v)
        except (TypeError, ValueError):
            logger.warning("cached_answer_lookup: invalid %s=%r, using default", key, v)
            return default

    def _bool(key, default):
        v = inputs.get(key)
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "y"}
        return default

    def _list(key):
        v = inputs.get(key)
        if not v:
            return None
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",") if p.strip()]
            return parts or None
        return None

    return {
        "similarity_floor": max(0.0, min(1.0, _num("similarity_floor", DEFAULT_SIMILARITY_FLOOR, float))),
        "max_age_days": _num("max_age_days", None, int),
        "domain_tags": _list("domain_tags"),
        "require_critic_approved": _bool("require_critic_approved", False),
        "require_no_thumbs_down": _bool("require_no_thumbs_down", DEFAULT_REQUIRE_NO_THUMBS_DOWN),
        "quality_score_floor": _num("quality_score_floor", 0.0, float),
        "top_k": max(1, min(20, _num("top_k", DEFAULT_TOP_K, int))),
        "config_sha": (inputs.get("config_sha") or "").strip() or None,
    }


def _build_chroma_where(filters: dict[str, Any]) -> dict | None:
    """Compose Chroma ``where`` clause from parsed filter inputs.

    Chroma's where syntax uses ``$and`` / ``$or`` / ``$in`` etc.; empty
    filter list returns None so we don't send an over-constrained
    query."""
    conditions: list[dict] = []
    if filters.get("require_critic_approved"):
        conditions.append({"critic_approved": True})
    if filters.get("require_no_thumbs_down"):
        # Chroma metadata is flat; we store thumbs_down as a bool.
        # ``$ne: True`` covers both False and absent-field cases.
        conditions.append({"thumbs_down": {"$ne": True}})
    if filters.get("quality_score_floor") and filters["quality_score_floor"] > 0:
        conditions.append({"quality_score": {"$gte": float(filters["quality_score_floor"])}})
    if filters.get("config_sha"):
        conditions.append({"config_sha": filters["config_sha"]})
    # domain_tags filtering happens post-query (Chroma's list-contains
    # semantics are clumsy across versions; cheaper to filter ~20 rows
    # in Python after retrieval than to encode a $in across comma-
    # joined strings).
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _age_days_of(iso_ts: str | None) -> float | None:
    """Convert ISO timestamp to age-in-days, tolerating missing / bad values."""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        return max(0.0, delta.total_seconds() / 86400.0)
    except Exception:
        return None


def _domain_tags_of(meta: dict) -> list[str]:
    raw = meta.get("domain_tags") or ""
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def _post_filter(
    candidates: list[dict[str, Any]],
    filters: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Apply similarity + age + domain-tag filtering post-retrieval.

    Returns (kept, reasons) where reasons is a count-by-reason of
    drops — so callers can see why a large recall shrunk (e.g.
    "all 20 Chroma matches were > 14 days old")."""
    kept: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    sim_floor = filters["similarity_floor"]
    max_age = filters.get("max_age_days")
    domain_tags = set(filters.get("domain_tags") or [])

    for c in candidates:
        if c["similarity"] < sim_floor:
            reasons["below_similarity_floor"] = reasons.get("below_similarity_floor", 0) + 1
            continue
        if max_age is not None and c.get("age_days") is not None and c["age_days"] > max_age:
            reasons["age_over_threshold"] = reasons.get("age_over_threshold", 0) + 1
            continue
        if domain_tags:
            cand_tags = set(_domain_tags_of(c.get("meta") or {}))
            if not (domain_tags & cand_tags):
                reasons["domain_tag_mismatch"] = reasons.get("domain_tag_mismatch", 0) + 1
                continue
        kept.append(c)

    return kept, reasons


# ── Rendering ──────────────────────────────────────────────────────────


def _render_candidate_for_text(idx: int, c: dict[str, Any]) -> str:
    """One candidate as a markdown-ish block for the reasoning LLM."""
    meta = c.get("meta") or {}
    age_d = c.get("age_days")
    age_str = f"{age_d:.1f}d" if isinstance(age_d, (int, float)) else "unknown age"
    sim = c.get("similarity", 0.0)
    q = (meta.get("question") or c.get("document") or "").strip()
    ans = (meta.get("final_message") or "").strip()
    approved = "critic-approved" if meta.get("critic_approved") else "not audited"
    src_count = meta.get("source_count") or 0
    return (
        f"[{idx}] similarity={sim:.3f} · age={age_str} · {approved} · sources={src_count}\n"
        f"    Q: {q[:220]}\n"
        f"    A: {ans[:500]}"
        + ("…" if len(ans) > 500 else "")
    )


def _render_envelope_text(kept: list[dict[str, Any]]) -> str:
    if not kept:
        return ""
    header = (
        "CACHED PRIOR ANSWERS (semantically similar past turns, returned for "
        "your judgment — verify freshness against fresh retrieval below before "
        "finalizing verbatim):\n"
    )
    body = "\n\n".join(_render_candidate_for_text(i + 1, c) for i, c in enumerate(kept))
    return f"{header}\n{body}"


# ── Handler ────────────────────────────────────────────────────────────


def _run(call: SkillCall) -> SkillEnvelope:
    """Neutral semantic-lookup handler. See module docstring for contract."""
    from app.services.embedding_provider import get_query_embedding

    filters = _parse_inputs(call.inputs)

    # Fallback: when question is absent (shouldn't happen but defensive)
    # take call.question; when both absent, return empty.
    query_text = (call.inputs or {}).get("question") if call.inputs else None
    query_text = (query_text or call.question or "").strip()
    if not query_text:
        return SkillEnvelope(
            text="",
            signal="no_sources",
            extra={"candidates": [], "reasons_filtered": {"empty_query": 1}},
        )

    try:
        coll = _get_cache_collection()
    except Exception as e:
        logger.warning("cache skill: collection unavailable: %s", e)
        return SkillEnvelope(
            text="",
            signal="no_sources",
            extra={
                "candidates": [],
                "reasons_filtered": {"collection_unavailable": 1},
                "error": str(e)[:200],
            },
        )

    try:
        embedding = get_query_embedding(query_text)
    except Exception as e:
        logger.warning("cache skill: embed failed: %s", e)
        return SkillEnvelope(
            text="",
            signal="no_sources",
            extra={
                "candidates": [],
                "reasons_filtered": {"embed_failed": 1},
                "error": str(e)[:200],
            },
        )

    where = _build_chroma_where(filters)
    # Over-fetch so post-filtering has room; cap at 40 to keep
    # Chroma round-trip predictable.
    n_chroma = min(40, max(filters["top_k"] * 4, 12))

    try:
        result = coll.query(
            query_embeddings=[embedding],
            n_results=n_chroma,
            where=where,
            include=["metadatas", "documents", "distances"],
        )
    except Exception as e:
        logger.warning("cache skill: Chroma query failed: %s", e)
        return SkillEnvelope(
            text="",
            signal="no_sources",
            extra={
                "candidates": [],
                "reasons_filtered": {"query_failed": 1},
                "error": str(e)[:200],
            },
        )

    ids = (result.get("ids") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    documents = (result.get("documents") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]

    candidates: list[dict[str, Any]] = []
    for i, cid in enumerate(ids):
        meta = metadatas[i] if i < len(metadatas) else {}
        doc = documents[i] if i < len(documents) else ""
        dist = float(distances[i]) if i < len(distances) else 1.0
        # Chroma cosine-space distances are 1 - cosine_similarity,
        # clamped to [0, 2]. Similarity = 1 - distance, clamped [0,1].
        similarity = max(0.0, min(1.0, 1.0 - dist))
        candidates.append(
            {
                "cache_turn_id": cid,
                "similarity": similarity,
                "document": doc,
                "meta": dict(meta or {}),
                "age_days": _age_days_of((meta or {}).get("created_at")),
            }
        )

    kept, reasons = _post_filter(candidates, filters)
    kept.sort(key=lambda c: c["similarity"], reverse=True)
    kept = kept[: filters["top_k"]]

    if not kept:
        return SkillEnvelope(
            text="",
            signal="no_sources",
            extra={
                "candidates": [],
                "reasons_filtered": reasons,
                "chroma_returned": len(candidates),
            },
        )

    sources: list[SourceRef] = []
    for idx, c in enumerate(kept, start=1):
        meta = c["meta"] or {}
        sources.append(
            SourceRef(
                document_name=f"cached_answer[{idx}]",
                index=idx,
                text=(meta.get("final_message") or "")[:1500],
                source_type="cached_answer",
                url=None,
            )
        )

    return SkillEnvelope(
        text=_render_envelope_text(kept),
        sources=sources,
        signal="cache_hit",
        extra={
            "candidates": [
                {
                    "cache_turn_id": c["cache_turn_id"],
                    "similarity": c["similarity"],
                    "age_days": c["age_days"],
                    "question": (c["meta"] or {}).get("question", ""),
                    "final_message": (c["meta"] or {}).get("final_message", "")[:500],
                    "critic_approved": bool((c["meta"] or {}).get("critic_approved")),
                    "thumbs_up": bool((c["meta"] or {}).get("thumbs_up")),
                    "source_count": int((c["meta"] or {}).get("source_count") or 0),
                    "config_sha": (c["meta"] or {}).get("config_sha"),
                    "domain_tags": _domain_tags_of(c["meta"] or {}),
                }
                for c in kept
            ],
            "reasons_filtered": reasons,
            "chroma_returned": len(candidates),
        },
    )


# ── Spec + registration ───────────────────────────────────────────────


SPEC = SkillSpec(
    name="cached_answer_lookup",
    description=(
        "cached_answer_lookup(question, similarity_floor?, max_age_days?, "
        "domain_tags?, require_critic_approved?, require_no_thumbs_down?, "
        "quality_score_floor?, top_k?, config_sha?)\n"
        "  Semantic lookup against prior completed turns (chat_answer_cache).\n"
        "  Use when: the user's question may have been answered before and a\n"
        "  recent cached answer could finalize this turn without new retrieval.\n"
        "  Do NOT use for: questions with explicit freshness markers ('today',\n"
        "  'current', 'latest') — those should always invoke fresh retrieval.\n"
        "  Returns: up to top_k candidates, each with similarity, age, quality\n"
        "  signals, original question, original answer, and original sources.\n"
        "  You (the reasoning LLM) decide whether to use them."
    ),
    handler=_run,
    inputs_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "similarity_floor": {"type": "number"},
            "max_age_days": {"type": "integer"},
            "domain_tags": {"type": "array", "items": {"type": "string"}},
            "require_critic_approved": {"type": "boolean"},
            "require_no_thumbs_down": {"type": "boolean"},
            "quality_score_floor": {"type": "number"},
            "top_k": {"type": "integer"},
            "config_sha": {"type": "string"},
        },
        "required": [],
    },
    requires_jurisdiction=False,
    follow_up_capable=False,
    supports_modes=("copilot", "quick"),
    source="builtin",
    visible_to_planner=True,
)


register(SPEC)
