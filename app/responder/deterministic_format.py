"""Deterministic (no-LLM) card formatter for Task #76 dynamic enrichment.

When react's own answer is already sufficient (see
app.pipeline.react_loop._is_sufficient_for_deterministic_pass), the
integrator skips Call A entirely and structures react_draft into the card
shape with regex only -- no synthesis, no model call. Chat Master's explicit
ruling: NO LLM fallback on this path. If the deterministic pass can't
confidently structure the content, it passes react_draft through as-is
(bolded, no sections) rather than guessing.

Two things this does, matching bad3d7b's presentation-enforcement rules
without an LLM:
1. Bold key facts (money, percentages, durations, dates) via regex.
2. Promote obvious "Label: Value" line pairs into a stats/table section --
   the "hero card" treatment -- when the source text already has that shape.
"""
from __future__ import annotations

import re
from typing import Any

_MONEY_RE = re.compile(r"\$[\d,]+(?:\.\d{1,2})?")
_PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?%")
_DURATION_RE = re.compile(
    r"\b\d+\s*(?:day|days|hour|hours|week|weeks|month|months|year|years)\b",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},?\s+\d{4}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
)

# Ordered so DATE (most specific) runs before the others -- a duration-shaped
# substring inside an already-matched date won't get double-wrapped since
# re.sub only rewrites the parts of the string outside prior replacements
# (each pass operates on the ALREADY-bolded text, but ** markers don't match
# any of these patterns, so a second pass can't re-wrap the same span).
_BOLD_PATTERNS = (_DATE_RE, _MONEY_RE, _PERCENT_RE, _DURATION_RE)


def bold_key_facts(text: str) -> str:
    """Wrap money/percentage/duration/date substrings in **bold**. Skips
    spans already inside ** markers so repeated patterns (e.g. a duration
    inside an already-bolded date-ish phrase) don't get double-wrapped."""
    if not text:
        return text
    out = text
    for pattern in _BOLD_PATTERNS:
        def _wrap(m: re.Match) -> str:
            start, end = m.span()
            # Already bolded (immediately preceded/followed by **) -- skip.
            if out[max(0, start - 2):start] == "**" or out[end:end + 2] == "**":
                return m.group(0)
            return f"**{m.group(0)}**"
        out = pattern.sub(_wrap, out)
    return out


_LABEL_VALUE_RE = re.compile(r"^([A-Za-z][\w\s/\-]{1,40}):\s*(.{1,100})$")


def _extract_label_value_pairs(text: str) -> list[tuple[str, str]]:
    """Consecutive "Label: Value" lines -- the pattern react_draft has when
    it already reads like structured notes rather than prose. Most
    react_draft text is synthesized prose and won't match; that's expected,
    not a bug -- those turns just get the bold-only treatment below."""
    pairs = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        m = _LABEL_VALUE_RE.match(line)
        if m:
            pairs.append((m.group(1).strip(), m.group(2).strip()))
    return pairs


def deterministic_format(react_draft: str | None) -> dict[str, Any]:
    """Structure react_draft into an AnswerCard-shaped dict with regex only.
    No LLM call. Returns {mode, direct_answer, sections}."""
    text = (react_draft or "").strip()
    if not text:
        return {"mode": "FACTUAL", "direct_answer": "", "sections": []}

    bolded = bold_key_facts(text)
    pairs = _extract_label_value_pairs(text)
    sections: list[dict[str, Any]] = []

    # 2-6 pairs is the "this is confidently structured" band -- fewer than 2
    # isn't a real pattern (one line matching the regex by coincidence), more
    # than 6 usually means the regex is over-matching prose sentences that
    # happen to contain a colon, not real label:value notes.
    if 2 <= len(pairs) <= 6:
        if all(len(v) <= 30 for _, v in pairs):
            sections.append({
                "intent": "process",
                "label": "Key Facts",
                "format": "stats",
                "data": {"items": [{"label": l, "value": v} for l, v in pairs]},
            })
        else:
            sections.append({
                "intent": "process",
                "label": "Details",
                "format": "table",
                "data": {"headers": ["Item", "Detail"], "rows": [[l, v] for l, v in pairs]},
            })

    return {"mode": "FACTUAL", "direct_answer": bolded, "sections": sections}
