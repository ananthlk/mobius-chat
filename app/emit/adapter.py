"""Map technical retrieval emits to user-friendly messages. Used by Emitter for retrieve stage."""
from __future__ import annotations

import logging
import os
import re
from typing import Callable

logger = logging.getLogger(__name__)

_DEBUG_ENABLED: bool | None = None


def _is_debug_retrieval_emits() -> bool:
    global _DEBUG_ENABLED
    if _DEBUG_ENABLED is not None:
        return _DEBUG_ENABLED
    for key in ("CHAT_DEBUG_RETRIEVAL_EMITS", "DEBUG_RETRIEVAL_EMITS"):
        v = (os.environ.get(key) or "").strip().lower()
        if v in ("1", "true", "yes", "on"):
            _DEBUG_ENABLED = True
            return True
    _DEBUG_ENABLED = False
    return False


def _normalize_using_results(m: re.Match[str]) -> str:
    n = int(m.group(1))
    word = "result" if n == 1 else "results"
    return f"Using {n} {word} to answer this part."


_USER_FRIENDLY_MAP: list[tuple[str, str | None | Callable[[re.Match[str]], str]]] = [
    (r"^Mobius path:.*", "Searching our materials..."),
    (r"^Lazy path:.*", "Searching our materials..."),
    (r"BM25:.*", None),
    (r"BM25 corpus:.*", None),
    (r"BM25 .* matches:.*", None),
    (r"BM25 returned.*", None),
    (r"Building BM25.*", None),
    (r"^Vertex returned.*", None),
    (r"^Searching Vertex\.\.\.$", None),
    (r"^Fetching .* metadata rows.*", None),
    (r"^Postgres returned.*", None),
    (r"JPD tagger:.*", None),
    (r"J/P/D tagger:.*", None),
    (r"^Corpus confidence sufficient.*", "Found strong matches in our materials."),
    (r"^Adding external search.*", "Adding external sources to complement what we found."),
    (r"^Low corpus confidence.*", "Searching the web for additional context."),
    (r"^Using (\d+) result\(?s?\)? to answer.*", _normalize_using_results),
]

_COMPILED = [(re.compile(pat), repl) for pat, repl in _USER_FRIENDLY_MAP]


def wrap_technical_for_user(msg: str, user_friendly: bool = True) -> str | None:
    """Map technical message to user-friendly, or None if omitted. Returns original when debug or no match."""
    s = (msg or "").strip()
    if not s:
        return None
    if not user_friendly:
        return s
    debug = _is_debug_retrieval_emits()
    for pat, repl in _COMPILED:
        m = pat.search(s)
        if m:
            if debug:
                logger.info("[retrieval] %s", s)
                return s
            if callable(repl):
                return repl(m)
            if repl is not None:
                return repl
            return None
    return s
