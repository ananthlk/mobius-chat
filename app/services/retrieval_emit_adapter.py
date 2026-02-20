"""Map technical retrieval emits to user-friendly messages for the thinking UI."""
from __future__ import annotations

import logging
import os
import re
from typing import Callable

logger = logging.getLogger(__name__)

# When CHAT_DEBUG_RETRIEVAL_EMITS=1, technical emits are logged and passed through (not omitted).
_DEBUG_RETRIEVAL_EMITS_KEYS = ("CHAT_DEBUG_RETRIEVAL_EMITS", "DEBUG_RETRIEVAL_EMITS")
_DEBUG_ENABLED: bool | None = None


def _is_debug_retrieval_emits() -> bool:
    global _DEBUG_ENABLED
    if _DEBUG_ENABLED is not None:
        return _DEBUG_ENABLED
    for key in _DEBUG_RETRIEVAL_EMITS_KEYS:
        v = (os.environ.get(key) or "").strip().lower()
        if v in ("1", "true", "yes", "on"):
            _DEBUG_ENABLED = True
            return True
    _DEBUG_ENABLED = False
    return False


def _normalize_using_results(m: re.Match[str]) -> str:
    """Normalize 'Using X result(s) to answer...' -> 'Using X results to answer this part.'"""
    n = int(m.group(1))
    word = "result" if n == 1 else "results"
    return f"Using {n} {word} to answer this part."


# Regex patterns -> replacement (None = omit, str = replace, callable = call with match)
_USER_FRIENDLY_MAP: list[tuple[str, str | None | Callable[[re.Match[str]], str]]] = [
    (r"^Mobius path:.*", "Searching our materials..."),
    (r"^Lazy path:.*", "Searching our materials..."),
    (r"^BM25:.*", None),
    (r"^BM25 corpus:.*", None),
    (r"^BM25 .* matches:.*", None),
    (r"^BM25 returned.*", None),
    (r"^Building BM25.*", None),
    (r"^Vertex returned.*", None),
    (r"^Searching Vertex\.\.\.$", None),
    (r"^Fetching .* metadata rows.*", None),
    (r"^Postgres returned.*", None),
    (r"^JPD tagger:.*", None),
    (r"^J/P/D tagger:.*", None),
    (r"J/P/D tagger:.*document\(s\)", None),  # e.g. "J/P/D tagger: -> 15 document(s) e.g. [...]"
    (r"^Corpus confidence sufficient.*", "Found strong matches in our materials."),
    (r"^Adding external search.*", "Adding external sources to complement what we found."),
    (r"^Low corpus confidence.*", "Searching the web for additional context."),
    (r"^Using (\d+) result\(?s?\)? to answer.*", _normalize_using_results),
]


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

    debug = _is_debug_retrieval_emits()
    _compiled = [(re.compile(pat), repl) for pat, repl in _USER_FRIENDLY_MAP]

    def _emit(msg: str) -> None:
        s = (msg or "").strip()
        if not s:
            return
        for pat, repl in _compiled:
            m = pat.search(s)
            if m:
                if debug:
                    logger.info("[retrieval] %s", s)
                    emitter(s)
                elif callable(repl):
                    emitter(repl(m))
                elif repl is not None:
                    emitter(repl)
                return
        # No match: pass through (e.g. "Using 3 results to answer this part.")
        emitter(s)

    return _emit


# Alias for plan compatibility
RetrievalEmitterAdapter = wrap_emitter_for_user
