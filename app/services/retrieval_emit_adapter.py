"""Map technical retrieval emits to user-friendly messages for the thinking UI.

Uses app.emit.adapter for the mapping logic (single source of truth).
"""
from __future__ import annotations

import logging
from typing import Callable

from app.emit.adapter import wrap_technical_for_user

logger = logging.getLogger(__name__)


def wrap_emitter_for_user(
    emitter: Callable[[str], None] | None,
    user_friendly: bool = True,
) -> Callable[[str], None]:
    """Wrap an emitter so technical messages are mapped to user-friendly ones (or omitted).

    When user_friendly=False: pass through all messages unchanged (for CLI/debug).
    When user_friendly=True: map technical emits to user-friendly; omit internal ones.

    When CHAT_DEBUG_RETRIEVAL_EMITS=1: technical emits are logged and passed through to the emitter
    for debugging (BM25 corpus, matches, JPD tagger, etc.).
    """
    if emitter is None:
        return lambda _: None

    if not user_friendly:
        return lambda msg: emitter(msg.strip()) if msg and msg.strip() else None

    def _emit(msg: str) -> None:
        s = (msg or "").strip()
        if not s:
            return
        mapped = wrap_technical_for_user(s, user_friendly=True)
        if mapped is not None:
            emitter(mapped)

    return _emit


# Alias for plan compatibility
RetrievalEmitterAdapter = wrap_emitter_for_user
