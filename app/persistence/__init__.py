"""Unified persistence: PersistencePort interface, Postgres and memory implementations."""

from app.persistence.interface import PersistencePort
from app.persistence.postgres import PostgresPersistence
from app.persistence.memory import MemoryPersistence

__all__ = ["PersistencePort", "PostgresPersistence", "MemoryPersistence", "get_persistence"]


def get_persistence() -> PersistencePort:
    """Return Postgres when DB configured, else Memory (explicit no-DB)."""
    try:
        from app.chat_config import get_chat_config
        url = (get_chat_config().rag.database_url or "").strip()
        if url:
            return PostgresPersistence()
    except Exception:
        pass
    return MemoryPersistence()
