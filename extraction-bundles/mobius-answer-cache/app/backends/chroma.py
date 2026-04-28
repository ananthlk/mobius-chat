"""Chroma backend (Phase 0).

Wraps the existing ``chat_answer_cache`` Chroma collection at the same
host chat used to talk to directly. The query / write / filter logic
is lifted from ``mobius-chat/app/skills/builtin/cached_answer.py`` and
``mobius-chat/app/services/cache_writer.py``.

This backend is intentionally a faithful reproduction of the chat-side
code — Phase 0 must not introduce any new behavior, just relocate the
existing one to a service. Bug-for-bug compat. Phase 1 (pgvector)
gets the cleanup + the new analytics surfaces.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.backends.base import CacheBackend

logger = logging.getLogger(__name__)


class ChromaBackend(CacheBackend):
    name = "chroma"

    def __init__(self) -> None:
        self._collection = None
        self._lock = threading.Lock()

    # ── Lazy connection ───────────────────────────────────────────────

    def _get_collection(self):
        if self._collection is not None:
            return self._collection
        with self._lock:
            if self._collection is not None:
                return self._collection
            import chromadb
            host = (os.environ.get("CHROMA_HOST") or "").strip()
            collection_name = (
                os.environ.get("CACHE_COLLECTION")
                or os.environ.get("CACHE_ASSIST_CHROMA_COLLECTION")
                or "chat_answer_cache"
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
            self._collection = client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info("chroma backend: connected to collection %s", collection_name)
            return self._collection

    # ── Lookup ────────────────────────────────────────────────────────

    def lookup(
        self,
        *,
        embedding: list[float],
        config_sha: str | None,
        filters: dict[str, Any],
        min_similarity: float,
        k: int,
    ) -> list[dict[str, Any]]:
        coll = self._get_collection()
        where = self._build_where(config_sha=config_sha, filters=filters)
        # Over-fetch so post-filtering has room.
        n_chroma = min(40, max(k * 4, 12))
        try:
            result = coll.query(
                query_embeddings=[embedding],
                n_results=n_chroma,
                where=where,
                include=["metadatas", "documents", "distances"],
            )
        except Exception as e:
            logger.warning("chroma lookup query failed: %s", e)
            return []
        ids = (result.get("ids") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        candidates: list[dict[str, Any]] = []
        for i, cid in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""
            dist = float(distances[i]) if i < len(distances) else 1.0
            similarity = max(0.0, min(1.0, 1.0 - dist))
            if similarity < min_similarity:
                continue
            age_days = self._age_days_of((meta or {}).get("created_at"))
            max_age = filters.get("max_age_days")
            if max_age is not None and age_days is not None and age_days > float(max_age):
                continue
            domain_filter = set(filters.get("domain_tags") or [])
            if domain_filter:
                cand_tags = set(self._domain_tags_of(meta or {}))
                if cand_tags and not (domain_filter & cand_tags):
                    continue
            try:
                envelope = json.loads(meta.get("skill_envelope") or "{}")
            except Exception:
                envelope = {}
            candidates.append({
                "candidate_id": str(cid),
                "question": (meta.get("question") or doc or "").strip(),
                "answer": (meta.get("final_message") or "").strip(),
                "skill_envelope": envelope,
                "similarity": similarity,
                "age_days": age_days,
                "config_sha": meta.get("config_sha"),
                "thumbs_down": bool(meta.get("thumbs_down")),
                "domain_tags": self._domain_tags_of(meta or {}),
                "thread_id": meta.get("thread_id"),
                "answered_at": meta.get("created_at"),
            })
        candidates.sort(key=lambda c: c["similarity"], reverse=True)
        return candidates[:k]

    @staticmethod
    def _build_where(*, config_sha: str | None, filters: dict[str, Any]) -> dict | None:
        conditions: list[dict] = []
        if filters.get("require_critic_approved"):
            conditions.append({"critic_approved": True})
        if filters.get("require_no_thumbs_down", True):
            conditions.append({"thumbs_down": {"$ne": True}})
        if filters.get("quality_score_floor") and filters["quality_score_floor"] > 0:
            conditions.append({"quality_score": {"$gte": float(filters["quality_score_floor"])}})
        if config_sha:
            conditions.append({"config_sha": config_sha})
        # payer/state/program filters: native equality if set on the row.
        for key in ("payer", "state", "program", "authority_level"):
            v = filters.get(key)
            if v:
                conditions.append({key: str(v)})
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}

    # ── Write ─────────────────────────────────────────────────────────

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
        coll = self._get_collection()
        # Idempotency: deterministic id from correlation_id so a replay
        # is an upsert, not a duplicate.
        candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"chat:{correlation_id}"))
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        meta = {
            "correlation_id": correlation_id,
            "thread_id": thread_id or "",
            "question": question[:2000],
            "final_message": answer[:8000],
            "skill_envelope": json.dumps(skill_envelope, default=str)[:64000],
            "config_sha": config_sha or "",
            "payer": (filters.get("payer") or "")[:100],
            "state": (filters.get("state") or "")[:10],
            "program": (filters.get("program") or "")[:100],
            "authority_level": (filters.get("authority_level") or "")[:50],
            "domain_tags": ",".join(domain_tags or []),
            "qc_passed": bool(qc_passed),
            "thumbs_down": bool(thumbs_down),
            "caller": caller or "unknown",
            "created_at": now_iso,
        }
        # Chroma upsert: delete-by-id then add. (Newer chromadb supports
        # ``upsert()`` directly; using add+ignore-on-duplicate for
        # compat with existing chat_answer_cache server version.)
        try:
            coll.delete(ids=[candidate_id])
        except Exception:
            pass
        coll.add(
            ids=[candidate_id],
            embeddings=[embedding],
            documents=[question[:2000]],
            metadatas=[meta],
        )
        return candidate_id

    # ── Mutations ─────────────────────────────────────────────────────

    def mark_thumbs_down(self, candidate_id: str, *, reason: str | None = None) -> None:
        coll = self._get_collection()
        # Chroma's update doesn't support partial metadata edits cleanly
        # — refetch and rewrite.
        existing = coll.get(ids=[candidate_id], include=["metadatas", "documents", "embeddings"])
        if not existing.get("ids"):
            return
        meta = (existing.get("metadatas") or [{}])[0] or {}
        meta["thumbs_down"] = True
        if reason:
            meta["thumbs_down_reason"] = reason[:200]
        coll.update(ids=[candidate_id], metadatas=[meta])

    def bulk_invalidate(self, *, filter: dict[str, Any]) -> int:
        coll = self._get_collection()
        where = self._build_where(config_sha=filter.get("config_sha"), filters=filter)
        if where is None:
            return 0
        try:
            existing = coll.get(where=where, include=["metadatas"])
            ids = existing.get("ids") or []
            if ids:
                coll.delete(ids=ids)
            return len(ids)
        except Exception as e:
            logger.warning("chroma bulk_invalidate failed: %s", e)
            return 0

    # ── History / analytics (best-effort on Chroma) ───────────────────

    def list_history(
        self,
        *,
        thread_id: str | None,
        caller: str | None,
        since: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        coll = self._get_collection()
        conditions: list[dict] = []
        if thread_id:
            conditions.append({"thread_id": thread_id})
        if caller:
            conditions.append({"caller": caller})
        where = None
        if len(conditions) == 1:
            where = conditions[0]
        elif len(conditions) > 1:
            where = {"$and": conditions}
        try:
            existing = coll.get(where=where, include=["metadatas", "documents"], limit=limit)
        except Exception:
            return []
        cutoff = self._cutoff_iso(since)
        rows: list[dict[str, Any]] = []
        for i, cid in enumerate(existing.get("ids") or []):
            meta = (existing.get("metadatas") or [{}])[i] or {}
            ts = meta.get("created_at")
            if cutoff and ts and ts < cutoff:
                continue
            rows.append({
                "candidate_id": str(cid),
                "question": meta.get("question", ""),
                "answer": meta.get("final_message", "")[:500],
                "answered_at": ts,
                "thread_id": meta.get("thread_id"),
                "caller": meta.get("caller"),
                "qc_passed": bool(meta.get("qc_passed")),
                "thumbs_down": bool(meta.get("thumbs_down")),
            })
        rows.sort(key=lambda r: r.get("answered_at") or "", reverse=True)
        return rows[:limit]

    def stats(self, *, since: str) -> dict[str, Any]:
        # Chroma doesn't expose row counts cheaply. Best-effort: scan
        # capped to 5000 rows in the window.
        coll = self._get_collection()
        try:
            existing = coll.get(include=["metadatas"], limit=5000)
        except Exception:
            return {"backend": self.name, "error": "stats_unavailable"}
        cutoff = self._cutoff_iso(since)
        n_rows = 0
        n_thumbs_down = 0
        callers: dict[str, int] = {}
        for meta in existing.get("metadatas") or []:
            ts = (meta or {}).get("created_at")
            if cutoff and ts and ts < cutoff:
                continue
            n_rows += 1
            if (meta or {}).get("thumbs_down"):
                n_thumbs_down += 1
            cl = (meta or {}).get("caller") or "unknown"
            callers[cl] = callers.get(cl, 0) + 1
        return {
            "backend": self.name,
            "since": since,
            "row_count": n_rows,
            "thumbs_down_count": n_thumbs_down,
            "callers": callers,
            "note": "chroma stats are best-effort; pgvector backend will give real counts",
        }

    def health_check(self) -> bool:
        try:
            coll = self._get_collection()
            coll.count()
            return True
        except Exception as e:
            logger.warning("chroma health check failed: %s", e)
            return False

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _age_days_of(iso_ts: str | None) -> float | None:
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

    @staticmethod
    def _domain_tags_of(meta: dict) -> list[str]:
        raw = meta.get("domain_tags") or ""
        return [t.strip() for t in str(raw).split(",") if t.strip()]

    @staticmethod
    def _cutoff_iso(since: str) -> str | None:
        if not since:
            return None
        unit = since[-1].lower()
        try:
            n = int(since[:-1])
        except (ValueError, TypeError):
            return None
        deltas = {"h": "hours", "d": "days", "m": "minutes", "w": "weeks"}
        if unit not in deltas:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(**{deltas[unit]: n})
        return cutoff.isoformat().replace("+00:00", "Z")
