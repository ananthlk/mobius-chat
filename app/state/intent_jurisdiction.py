"""Intent/jurisdiction separation: strip jurisdiction phrases from intent using J-tag lexicon.

Flow:
- Tagger finds j_tags in text
- Lexicon provides phrases/aliases for those j_tags
- We strip those phrases from intent to get clean intent
- Recombine with jurisdiction at retrieval time
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Patterns to clean after stripping (trailing fragments)
_STRIP_AFTER_REMOVE = re.compile(
    r"\s+(?:for|in|and|,|\(|\))\s*$|^\s*(?:for|in|and|,|\(|\))\s+",
    re.I,
)


# Jurisdictional context: only strip phrase when it appears after these (avoids "program" in "care management program")
_JURISDICTION_PREFIXES = (" for ", " in ", " with ", ", ", " (")


def _phrase_pattern(phrase: str) -> re.Pattern | None:
    """Build pattern that matches prefix+phrase so we remove ' for Sunshine Health' etc."""
    if not phrase or not phrase.strip():
        return None
    p = re.escape(phrase.strip())
    # Match " for X", " in X", etc.
    alt = "|".join(re.escape(pre) + p for pre in _JURISDICTION_PREFIXES)
    return re.compile(alt, re.I)


def strip_jurisdiction_from_intent(
    intent: str,
    rag_database_url: str | None = None,
    *,
    j_tags: dict[str, float] | None = None,
) -> str:
    """Remove jurisdiction phrases from intent using J-tag lexicon.

    Uses extract_tags_from_text to find j_tags, then get_phrases_for_j_tags to get
    phrases to strip. Removes them (case-insensitive, longer first) and cleans trailing fragments.

    Args:
        intent: Raw intent text that may contain jurisdiction (e.g. "what is X for Sunshine Health")
        rag_database_url: RAG DB URL for lexicon. If None, uses get_chat_config().rag.database_url.
        j_tags: Optional pre-extracted j_tags. If provided, skips extract_tags_from_text.

    Returns:
        Clean intent with jurisdiction phrases removed.
    """
    intent = (intent or "").strip()
    if not intent:
        return intent

    try:
        from mobius_retriever.jpd_tagger import (
            extract_tags_from_text,
            get_phrases_for_j_tags,
        )
    except ImportError:
        logger.debug("[intent_jurisdiction] mobius_retriever not available; skipping strip")
        return intent

    url = (rag_database_url or "").strip()
    if not url:
        try:
            from app.chat_config import get_chat_config
            url = (get_chat_config().rag.database_url or "").strip()
        except Exception:
            pass
    if not url:
        logger.debug("[intent_jurisdiction] RAG database URL not set; skipping strip")
        return intent

    if j_tags is None:
        result = extract_tags_from_text(intent, url, kinds=("j",))
        j_tags = result.get("j_tags") or {}
    if not j_tags:
        return intent

    phrases = get_phrases_for_j_tags(j_tags, url)
    if not phrases:
        return intent

    out = intent
    for phrase in phrases:
        pat = _phrase_pattern(phrase)
        if pat:
            out = pat.sub(" ", out)
    # Collapse multiple spaces, trim, remove trailing " for ", " in ", etc.
    out = " ".join(out.split()).strip()
    out = _STRIP_AFTER_REMOVE.sub("", out).strip()
    return out if out else intent
