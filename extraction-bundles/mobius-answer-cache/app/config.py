"""Centralized env reader. Mostly noise — kept minimal so the new
agent doesn't inherit chat's config sprawl. Add knobs here as
needed; do NOT fan them across modules.
"""
from __future__ import annotations

import os


# ── Backend ──────────────────────────────────────────────────────────

BACKEND = (os.environ.get("BACKEND") or "chroma").strip().lower()


# ── Chroma (Phase 0) ─────────────────────────────────────────────────

CHROMA_HOST = (os.environ.get("CHROMA_HOST") or "").strip()
CHROMA_PORT = int((os.environ.get("CHROMA_PORT") or "8000").strip())
CHROMA_SSL = (os.environ.get("CHROMA_SSL") or "").strip().lower() in {"1", "true", "yes"}
CHROMA_AUTH_TOKEN = (os.environ.get("CHROMA_AUTH_TOKEN") or "").strip()
CACHE_COLLECTION = (
    os.environ.get("CACHE_COLLECTION")
    or os.environ.get("CACHE_ASSIST_CHROMA_COLLECTION")
    or "chat_answer_cache"
)


# ── pgvector (Phase 1+) ──────────────────────────────────────────────

CACHE_DATABASE_URL = (os.environ.get("CACHE_DATABASE_URL") or "").strip()


# ── Embedding ────────────────────────────────────────────────────────

VERTEX_PROJECT_ID = (os.environ.get("VERTEX_PROJECT_ID") or "").strip()
VERTEX_LOCATION = (os.environ.get("VERTEX_LOCATION") or "us-central1").strip()
EMBEDDING_MODEL = (os.environ.get("EMBEDDING_MODEL") or "gemini-embedding-001").strip()


# ── Defaults / policy ────────────────────────────────────────────────

# Lookup defaults — overridable per-request in the body.
DEFAULT_MIN_SIMILARITY = float(os.environ.get("DEFAULT_MIN_SIMILARITY") or "0.85")
DEFAULT_MAX_AGE_DAYS = int(os.environ.get("DEFAULT_MAX_AGE_DAYS") or "14")
DEFAULT_K = int(os.environ.get("DEFAULT_K") or "5")
