"""Deterministic (no-LLM) card formatter for Task #76 dynamic enrichment.

When react's own answer is already sufficient (see
app.pipeline.react_loop._is_sufficient_for_deterministic_pass), the
integrator skips Call A entirely and structures react_draft into the card
shape with regex only -- no synthesis, no model call. Chat Master's explicit
ruling: NO LLM fallback on this path. If the deterministic pass can't
confidently structure the content, it passes react_draft through as-is
(bolded, no sections) rather than guessing.

2026-08-10 (Ananth via Chat FE, ReAct agreed no upstream shape signal exists
to reuse instead): the fast path only ever emitted plain prose beyond the
narrow label:value case, unlike the integrator which can pick table/stats/
bullets. Extended to detect the shapes react_draft's own formatting rules
already tend to produce (REACT_FORMAT_RULES_TEXT asks for bold-lead +
bullets) -- this is pattern-matching structure already in the text, not new
classification. Section vocabulary/data shapes locked with Chat FE
(bubble.ts _renderSectionBody) so these render with zero FE changes.

Things this does, matching bad3d7b's presentation-enforcement rules without
an LLM:
1. Bold key facts (money, percentages, durations, dates) via regex.
2. Markdown pipe-table -> format:"table".
3. 3+ consecutive markdown bullet lines -> format:"bullets" (top-level
   sec["bullets"], not sec["data"] -- FE reads that field name specifically).
4. 2+ consecutive numbered "Step N:" / "N." lines -> format:"steps".
5. "Label: Value" line pairs -> format:"stats" (<=4 pairs) or "table" (5+) --
   count-based split per Chat FE (stats tiles cap at 4 on the FE side).

Checked in priority order (most structurally specific first) so a draft
matching more than one pattern doesn't emit redundant/conflicting sections;
only the first confident match is used, the rest of the text stays as bolded
prose in direct_answer either way (sections are additive, never a rewrite of
direct_answer -- same as the original label:value behavior)."""
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


_MD_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$")
_MD_TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")
_HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _clean_table_cell(cell: str) -> str:
    """Markdown table cells can't contain a real newline, so a model
    writing a multi-value cell (e.g. "Participating: 180 days <br>
    Non-Participating: 365 days") reaches for literal HTML <br> as the
    only separator markdown syntax allows -- but this cell ships as a
    plain string in the typed table block, not through an HTML renderer,
    so the tag shows up raw and unrendered to the user (live finding,
    2026-08-12, Chat Master/Ananth). Replace with a plain-text separator
    that reads correctly regardless of whether the cell renderer supports
    embedded newlines."""
    parts = _HTML_BR_RE.split(cell)
    if len(parts) == 1:
        return cell.strip()
    return "; ".join(p.strip() for p in parts if p.strip())


def _extract_markdown_table(text: str) -> dict[str, Any] | None:
    """A markdown pipe table (header row, `---` separator, 1+ data rows) --
    react_draft can contain this verbatim when the tool output already had
    tabular shape. Returns {headers, rows} or None if no confident table."""
    lines = [ln.strip() for ln in (text or "").split("\n")]
    for i in range(len(lines) - 2):
        header_m = _MD_TABLE_ROW_RE.match(lines[i])
        if not header_m or not _MD_TABLE_SEP_RE.match(lines[i + 1]):
            continue
        headers = [_clean_table_cell(c) for c in header_m.group(1).split("|")]
        rows: list[list[str]] = []
        j = i + 2
        while j < len(lines):
            row_m = _MD_TABLE_ROW_RE.match(lines[j])
            if not row_m:
                break
            rows.append([_clean_table_cell(c) for c in row_m.group(1).split("|")])
            j += 1
        if rows:
            return {"headers": headers, "rows": rows}
    return None


_BULLET_LINE_RE = re.compile(r"^[-*]\s+(.+)$")


def _extract_bullets(text: str) -> list[str]:
    """3+ consecutive markdown bullet lines -- fewer isn't confidently a
    list (could be one stray dash in prose)."""
    lines = [ln.strip() for ln in (text or "").split("\n")]
    best: list[str] = []
    current: list[str] = []
    for ln in lines:
        m = _BULLET_LINE_RE.match(ln)
        if m:
            current.append(m.group(1).strip())
        else:
            if len(current) > len(best):
                best = current
            current = []
    if len(current) > len(best):
        best = current
    return best if len(best) >= 3 else []


_STEP_LINE_RE = re.compile(
    r"^(?:Step\s+\d+\s*[:.]\s*|\d+[.)]\s+)(.+)$", re.IGNORECASE,
)


def _extract_steps(text: str) -> list[str]:
    """2+ consecutive numbered "Step N:" or "N." lines -- an ordered
    procedure, distinct from a plain bullet list (order matters)."""
    lines = [ln.strip() for ln in (text or "").split("\n")]
    best: list[str] = []
    current: list[str] = []
    for ln in lines:
        m = _STEP_LINE_RE.match(ln)
        if m:
            current.append(m.group(1).strip())
        else:
            if len(current) > len(best):
                best = current
            current = []
    if len(current) > len(best):
        best = current
    return best if len(best) >= 2 else []


_RAW_EXCERPT_RE = re.compile(r"^\[\d+\]\s")


def _looks_like_raw_excerpt(text: str) -> bool:
    """react's thin-evidence fast-mode hedge (_build_fast_mode_hedge) ships a
    literal retrieved-chunk excerpt verbatim, not synthesized prose, exactly
    to avoid the appearance of confident synthesis when evidence is thin.
    Real policy text often has genuine "Label: Value" lines -- promoting
    those into a "Key Facts" stats card would misrepresent an unvetted
    excerpt as something we structured with confidence. Same defensive
    posture as _looks_like_raw_structured_blob (react_loop.py) guarding the
    JSON case -- this guards the plain-text excerpt case (2026-08-10, ReAct
    agent's real-sample audit)."""
    return bool(_RAW_EXCERPT_RE.match((text or "").strip()))


def deterministic_format(react_draft: str | None) -> dict[str, Any]:
    """Structure react_draft into an AnswerCard-shaped dict with regex only.
    No LLM call. Returns {mode, direct_answer, sections}."""
    text = (react_draft or "").strip()
    if not text:
        return {"mode": "FACTUAL", "direct_answer": "", "sections": []}

    bolded = bold_key_facts(text)
    sections: list[dict[str, Any]] = []

    if _looks_like_raw_excerpt(text):
        return {"mode": "FACTUAL", "direct_answer": bolded, "sections": sections}

    # Priority order: most structurally specific/unambiguous pattern first.
    # Only one section is emitted per draft -- the rest of the text stays as
    # bolded prose in direct_answer regardless (sections are additive).
    md_table = _extract_markdown_table(text)
    bullets = _extract_bullets(text)
    steps = _extract_steps(text)
    pairs = _extract_label_value_pairs(text)

    if md_table:
        sections.append({
            "intent": "process",
            "label": "Details",
            "format": "table",
            "data": md_table,
        })
    elif bullets:
        sections.append({
            "intent": "process",
            "label": "Key Points",
            "format": "bullets",
            "bullets": bullets,
        })
    elif steps:
        sections.append({
            "intent": "process",
            "label": "Steps",
            "format": "steps",
            "data": {"items": [{"label": s} for s in steps]},
        })
    # 2-6 pairs is the "this is confidently structured" band -- fewer than 2
    # isn't a real pattern (one line matching the regex by coincidence), more
    # than 6 usually means the regex is over-matching prose sentences that
    # happen to contain a colon, not real label:value notes. Within that
    # band: stats tiles need BOTH short values (a tile isn't a sentence) AND
    # <=4 pairs (the FE's stats-tile cap) -- either long values or 5+ pairs
    # goes to a table instead.
    elif 2 <= len(pairs) <= 6:
        if len(pairs) <= 4 and all(len(v) <= 30 for _, v in pairs):
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
