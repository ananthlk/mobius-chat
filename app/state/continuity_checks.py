"""Continuity checks: user-provided context, end pursuit.

See docs/RELENTLESS_CONTINUITY_PLAN.md.
"""
from __future__ import annotations

import re
from typing import Any

# Phrases that indicate the user wants to stop the relentless pursuit
END_PURSUIT_PHRASES = (
    r"\bnever\s*mind\b",
    r"\bthat'?s\s+enough\b",
    r"\bstop\b",
    r"\bi'?m\s+done\b",
    r"\bno\s+thanks?\b",
    r"\bcancel\b",
    r"\bforget\s+it\b",
    r"\bdon'?t\s+(worry|bother)\b",
    r"\bthat'?s\s+ok(ay)?\b",
    r"\bskip\s+it\b",
    r"\bend\s+(the\s+)?search\b",
    r"\bthat'?s\s+all\b",
    r"\bno\s+more\b",
)
_END_PATTERN = re.compile("|".join(f"({p})" for p in END_PURSUIT_PHRASES), re.IGNORECASE)

# Phrases that indicate the user is providing information (not asking a new question)
USER_PROVIDES_INFO_PHRASES = (
    r"here'?s\s+(what\s+)?(i\s+)?found",
    r"i\s+found\s+(that|this)",
    r"here'?s\s+the\s+",
    r"for\s+your\s+reference",
    r"according\s+to\s+(the\s+)?",
    r"the\s+(manual|handbook|policy)\s+says",
    r"i\s+(have|found)\s+a\s+(link|document|pdf)",
    r"here'?s\s+a\s+link",
    r"this\s+(might|may)\s+help",
    r"i\s+think\s+(the\s+)?(codes?|code)\s+",
    r"i\s+think\s+it'?s?\s+[A-Z0-9]",
    r"the\s+codes?\s+(are|is)\s+",
    r"it'?s?\s+(H\d{4}|Z\d{2})",
)
_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_USER_PROVIDES_PATTERN = re.compile(
    "|".join(f"({p})" for p in USER_PROVIDES_INFO_PHRASES), re.IGNORECASE
)


def user_wants_to_end_pursuit(message: str) -> bool:
    """Return True if the user message indicates they want to stop the relentless pursuit."""
    if not message or not message.strip():
        return False
    return bool(_END_PATTERN.search(message.strip()))


def extract_user_provided_context(
    message: str,
    has_active_objective: bool = False,
) -> str | None:
    """If the user appears to be providing information (not ending pursuit), return their message as context. Else None."""
    if not message or not message.strip():
        return None
    if user_wants_to_end_pursuit(message):
        return None
    text = message.strip()
    if _USER_PROVIDES_PATTERN.search(text):
        return text
    if _URL_PATTERN.search(text) and len(text) > 20:
        return text
    if has_active_objective and len(text) > 80 and "?" not in text[:100]:
        return text
    return None
