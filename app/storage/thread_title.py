"""Rule-based thread title generation (Phase 2.3).

Motivation
----------
The chat sidebar was rendering every ``chat_turns.question`` verbatim as a
"helpful search" entry — which meant users saw raw URLs, ICD codes with no
context, and tool-invocation fragments rather than an understandable list of
prior conversations. This module produces a short, human-readable title for
a thread, derived from its first user message.

Keeping this rule-based on purpose: an LLM summary is strictly better but
would add per-thread cost and latency on save. Rules are deterministic,
testable, and get us most of the way. An LLM upgrade can replace
``generate_thread_title`` later without touching the callers.
"""

from __future__ import annotations

import re

MAX_TITLE_CHARS = 60
MIN_TITLE_CHARS = 4

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")  # [text](url) → text
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_markup(text: str) -> str:
    """Drop code blocks, convert [text](url) to text, drop stray backticks and URLs."""
    t = _CODE_FENCE_RE.sub(" ", text)
    t = _MARKDOWN_LINK_RE.sub(r"\1", t)
    t = _INLINE_CODE_RE.sub(r"\1", t)
    t = _URL_RE.sub(" ", t)
    return t


def _truncate_on_word_boundary(text: str, limit: int) -> str:
    """Truncate ``text`` to at most ``limit`` chars, preferring word boundaries."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # If we're in the middle of a word, back off to the last space.
    space = cut.rfind(" ")
    if space >= limit // 2:  # don't back off more than half the budget
        cut = cut[:space]
    return cut.rstrip(" .,;:!-") + "…"


def is_noise(question: str) -> bool:
    """True if the raw question is too low-signal to become a thread title.

    Examples that should be flagged as noise:
    - Only a URL (user pasted a link)
    - Only a code (ICD/HCPCS/CPT with no surrounding question)
    - Too short (< 4 chars after stripping)
    - Too long (> 2 kB — this is almost certainly a tool-invocation dump)
    """
    q = (question or "").strip()
    if not q:
        return True
    if len(q) > 2000:
        return True
    stripped = _strip_markup(q).strip()
    if len(stripped) < MIN_TITLE_CHARS:
        return True
    # Pure HCPCS/CPT/ICD code with no context (e.g. "H0036", "F32.1") — useful
    # lookup but not a great thread title on its own.
    if re.fullmatch(r"[A-Z]\d{2,4}(?:\.\d+)?", q):
        return True
    return False


def generate_thread_title(question: str) -> str:
    """Produce a short, user-readable title from a raw user question.

    Rules:
    1. Strip code fences, inline code, markdown links → visible text.
    2. Remove URLs.
    3. Collapse whitespace.
    4. Truncate to ``MAX_TITLE_CHARS`` on a word boundary.
    5. If the result is empty or ``is_noise(question)`` was true, fall back to
       a timestamp-agnostic generic title so the UI never shows a blank row.

    This function is pure and idempotent — safe to call from the hot path.
    """
    if not question:
        return "Untitled chat"
    if is_noise(question):
        # Even when the original is noise, try to salvage something —
        # e.g. a lone URL's domain, a lone code rendered as "Lookup: H0036".
        q = (question or "").strip()
        url_match = _URL_RE.search(q)
        if url_match:
            url = url_match.group(0)
            # Extract domain for a human-readable title.
            m = re.search(r"https?://([^/?#]+)", url, re.IGNORECASE)
            if m:
                return _truncate_on_word_boundary(f"Lookup: {m.group(1)}", MAX_TITLE_CHARS)
        if re.fullmatch(r"[A-Z]\d{2,4}(?:\.\d+)?", q):
            return f"Lookup: {q}"
        return "Untitled chat"

    cleaned = _strip_markup(question).strip()
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    # Drop surrounding quotes if the whole thing was quoted.
    cleaned = cleaned.strip("\"'“”‘’ ")
    if not cleaned:
        return "Untitled chat"
    title = _truncate_on_word_boundary(cleaned, MAX_TITLE_CHARS)
    # Capitalize the first letter for consistency; leave the rest alone.
    if title and title[0].islower():
        title = title[0].upper() + title[1:]
    return title or "Untitled chat"
