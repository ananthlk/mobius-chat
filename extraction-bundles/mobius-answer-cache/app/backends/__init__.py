"""Backend selector. ``BACKEND`` env decides which implementation
get_backend() returns.

Phase 0:  BACKEND=chroma          (default; existing chat_answer_cache VM)
Phase 1:  BACKEND=dual            (Chroma + pgvector dual-write)
Phase 2:  BACKEND=pgvector_primary (pgvector authoritative; Chroma read-fallback)
Phase 3:  BACKEND=pgvector        (pgvector only)

The interface is in app/backends/base.py. Each concrete backend
implements it independently — get_backend() doesn't import the others
unless its env says to.
"""
from __future__ import annotations

import os
import threading

from app.backends.base import CacheBackend

_BACKEND: CacheBackend | None = None
_LOCK = threading.Lock()


def _resolve_name() -> str:
    return (os.environ.get("BACKEND") or "chroma").strip().lower()


def get_backend() -> CacheBackend:
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND
    with _LOCK:
        if _BACKEND is not None:
            return _BACKEND
        name = _resolve_name()
        if name == "chroma":
            from app.backends.chroma import ChromaBackend
            _BACKEND = ChromaBackend()
        elif name in ("pgvector", "pgvector_primary"):
            from app.backends.pgvector import PgVectorBackend
            _BACKEND = PgVectorBackend()
        elif name == "dual":
            from app.backends.chroma import ChromaBackend
            from app.backends.dual import DualBackend
            from app.backends.pgvector import PgVectorBackend
            _BACKEND = DualBackend(
                primary=ChromaBackend(),
                secondary=PgVectorBackend(),
            )
        else:
            raise RuntimeError(f"Unknown BACKEND={name!r}; expected one of chroma|dual|pgvector|pgvector_primary")
        return _BACKEND


def _reset_for_tests() -> None:
    global _BACKEND
    with _LOCK:
        _BACKEND = None
