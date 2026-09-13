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
direct_answer -- same as the original label:value behavior).

2026-09-12 (ENVELOPE_CLASSIFIER_SPEC.md, Phase 1): this module no longer
decides which envelope wins. It extracts shapes out of prose into a typed
ContentPayload and hands that to app.responder.envelope_classifier, which is
the one place in the codebase permitted to choose a format. The priority
order described above now lives in that ladder, unchanged -- the three named
precedence tests below (bullets > steps > pairs) pin it. What changed is that
the enricher path and the tool-hint path consult the same ladder, so the same
content can no longer render differently depending on which path served the
turn."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from mobius_contracts.taxonomies.envelope_thresholds import BULLETS_MIN_ITEMS

from app.responder.envelope_classifier import (
    MULTI_SECTION_ENABLED,
    ContentPayload,
    Item,
    RenderBudget,
    TableData,
    apply_render_budget,
    build_section,
    classify_envelope,
    log_verdict,
)

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

# A bullet whose text opens with a short label and a colon. react writes these
# unprompted -- "Dedicated Team: The Care Management team includes..." -- when
# the answer has a label/value shape and the format rules give it nowhere to
# put one. Seen live (cid 9c825ca5): five labelled items averaging 31 words,
# which the bullet path threw away as an over-long list and abstained on.
#
# The label is the row, the rest is the cell. Bold markers are stripped from
# the label because react's format rules ask for bold on entity names, and a
# row header carrying ** is markup leaking into data.
_LABELLED_BULLET_RE = re.compile(r"^\*{0,2}([A-Za-z][\w\s/()&\-]{1,40})\*{0,2}:\s+(.+)$")


def _as_labelled_pairs(items: list[str]) -> list[tuple[str, str]] | None:
    """A bullet run where EVERY item is "Label: value" is a definition list,
    not a bullet list.

    Every item, not most: one labelled line among unlabelled ones is a
    sentence with a colon, and promoting the run on that basis would put
    prose in a table cell and half the rows would have empty labels. All or
    nothing is the only threshold that cannot half-fire.
    """
    if len(items) < BULLETS_MIN_ITEMS:
        return None
    pairs: list[tuple[str, str]] = []
    for item in items:
        m = _LABELLED_BULLET_RE.match(item.strip())
        if not m:
            return None
        pairs.append((m.group(1).strip(), m.group(2).strip()))
    return pairs


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


def extract_payload(text: str) -> ContentPayload:
    """Prose -> typed content. Extraction only: this finds every shape present
    in the draft and asserts nothing about which one should win. A draft
    carrying both a bullet list and a colon line populates both fields;
    resolving that collision is the classifier's job, not the extractor's.

    All four extractors run unconditionally rather than short-circuiting on
    the first hit, because the ladder needs the full shape picture to make --
    and to trace -- its decision.
    """
    md_table = _extract_markdown_table(text)
    bullets = _extract_bullets(text)
    steps = _extract_steps(text)
    pairs = _extract_label_value_pairs(text)

    # Bullets and steps are both "items" to the classifier. Prose cannot
    # express an explicit list AND an ordered procedure over the same run of
    # lines, so at most one of these is real; where a draft contains both
    # runs, bullets are populated -- preserving the precedence pinned by
    # test_bullets_take_priority_over_label_value_pairs and
    # test_steps_take_priority_over_label_value_pairs_but_not_bullets.
    labelled = _as_labelled_pairs(bullets) if bullets else None
    if labelled:
        # A definition list. The pairs field carries it, so the ladder routes
        # it on content shape rather than on the bullet markers it happens to
        # be written with.
        return ContentPayload(
            text=text,
            table=(
                TableData(
                    headers=tuple(md_table["headers"]),
                    rows=tuple(tuple(r) for r in md_table["rows"]),
                )
                if md_table
                else None
            ),
            pairs=tuple(labelled),
            contiguous=True,
        )

    if bullets:
        items = tuple(Item(label=b) for b in bullets)
        explicit_list, ordered = True, False
    elif steps:
        items = tuple(Item(label=s) for s in steps)
        explicit_list, ordered = False, True
    else:
        items, explicit_list, ordered = (), False, False

    return ContentPayload(
        text=text,
        table=(
            TableData(
                headers=tuple(md_table["headers"]),
                rows=tuple(tuple(r) for r in md_table["rows"]),
            )
            if md_table
            else None
        ),
        pairs=tuple(pairs),
        items=items,
        explicit_list=explicit_list,
        ordered=ordered,
    )


_BLANK_RE = re.compile(r"^\s*$")


@dataclass(frozen=True)
class Segmentation:
    """What segment_draft found: the structural blocks, the draft's lines, and
    which line range each block came from.

    The SPANS are why this carries lines rather than a finished prose string.
    A block that is classified and then declines to render (an abstain --
    paragraph-length bullets, a lone label:value pair) must leave its content
    in the prose, or the answer simply loses it: measured 2026-09-12, a draft
    with a table plus a long-bullet block rendered the table and dropped three
    paragraphs of real content on the floor, because the prose was computed
    from what segmentation CONSUMED rather than from what actually rendered.

    Segmentation cannot know which blocks will render -- that is the
    classifier's answer, and it comes later. So it hands back the raw material
    and lets the caller subtract only what it actually used.
    """
    blocks: tuple[ContentPayload, ...] = ()
    lines: tuple[str, ...] = ()
    spans: tuple[tuple[int, int], ...] = ()

    @property
    def prose(self) -> str:
        """Everything outside every block. The view to use when all blocks
        rendered, and the conservative default."""
        return self.prose_excluding(range(len(self.blocks)))

    def prose_excluding(self, block_indices) -> str:
        """Everything outside the blocks named -- i.e. the ones that rendered.
        A block left out of `block_indices` keeps its lines in the prose."""
        consumed: set[int] = set()
        for idx in block_indices:
            if 0 <= idx < len(self.spans):
                consumed.update(range(*self.spans[idx]))
        return _remaining_prose(list(self.lines), consumed)


def segment_draft(text: str) -> Segmentation:
    """Split a draft into contiguous structural blocks plus the prose between
    them.

    The prose remainder is the point of returning a Segmentation rather than
    just a list. Sections have always been ADDITIVE to direct_answer, which
    was harmless when at most one section rendered and most of the draft was
    narrative. Once every block becomes its own section, the unsplit draft
    means the user reads the same table rows, the same steps and the same
    phone numbers twice -- once as prose, once as cards. Handing back what is
    left after the blocks are lifted out is what lets direct_answer say the
    things the cards cannot.

    Long answers carry more than one shape. A COB explainer has a deadline
    table AND a numbered submission procedure AND a block of contact details;
    the single-section path renders the table and silently drops the other
    two (measured: 2 of 3 shapes lost on real multi-shape drafts,
    2026-09-12). The content is not summarized away -- it stays in
    direct_answer as prose -- but the structure the author put there is
    thrown out, which is under-formatting rather than the over-formatting
    these systems usually get accused of.

    Every block is marked contiguous: a run of lines that all match the same
    pattern is structure by construction, which is what lets the pairs
    ceiling lift.
    """
    lines = (text or "").split("\n")
    blocks: list[ContentPayload] = []
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(lines)

    while i < n:
        stripped = lines[i].strip()

        if _BLANK_RE.match(stripped):
            i += 1
            continue

        # Pipe table: header + separator + 1..n rows.
        if (
            i + 1 < n
            and _MD_TABLE_ROW_RE.match(stripped)
            and _MD_TABLE_SEP_RE.match(lines[i + 1].strip())
        ):
            headers = [_clean_table_cell(c) for c in _MD_TABLE_ROW_RE.match(stripped).group(1).split("|")]
            rows: list[tuple[str, ...]] = []
            j = i + 2
            while j < n:
                row_m = _MD_TABLE_ROW_RE.match(lines[j].strip())
                if not row_m:
                    break
                rows.append(tuple(_clean_table_cell(c) for c in row_m.group(1).split("|")))
                j += 1
            if rows:
                blocks.append(ContentPayload(
                    table=TableData(headers=tuple(headers), rows=tuple(rows)),
                    contiguous=True,
                ))
                spans.append((i, j))
                i = j
                continue

        # Bullet run.
        if _BULLET_LINE_RE.match(stripped):
            run: list[str] = []
            j = i
            while j < n and _BULLET_LINE_RE.match(lines[j].strip()):
                run.append(_BULLET_LINE_RE.match(lines[j].strip()).group(1).strip())
                j += 1
            labelled_run = _as_labelled_pairs(run)
            if labelled_run:
                blocks.append(ContentPayload(
                    pairs=tuple(labelled_run), contiguous=True))
            else:
                blocks.append(ContentPayload(
                    items=tuple(Item(label=b) for b in run),
                    explicit_list=True,
                    contiguous=True,
                ))
            spans.append((i, j))
            i = j
            continue

        # Step run. Checked after bullets so "1. text" inside a bullet run
        # cannot split it.
        if _STEP_LINE_RE.match(stripped):
            run = []
            j = i
            while j < n and _STEP_LINE_RE.match(lines[j].strip()):
                run.append(_STEP_LINE_RE.match(lines[j].strip()).group(1).strip())
                j += 1
            blocks.append(ContentPayload(
                items=tuple(Item(label=s) for s in run),
                ordered=True,
                contiguous=True,
            ))
            spans.append((i, j))
            i = j
            continue

        # Label:value run. Last because its pattern is the loosest of the
        # four: "Step 1: Gather documentation" satisfies _LABEL_VALUE_RE as
        # readily as "Deadline: 180 days" does, so a run started on a genuine
        # pair line will swallow the numbered procedure that follows it and
        # emit the whole thing as stats. The run therefore stops at any line
        # a more specific extractor claims -- checking order at the top of
        # the loop is not enough on its own, because greed happens inside the
        # run, not at its start.
        if _LABEL_VALUE_RE.match(stripped):
            run_pairs: list[tuple[str, str]] = []
            j = i
            while j < n:
                candidate = lines[j].strip()
                if _BULLET_LINE_RE.match(candidate) or _STEP_LINE_RE.match(candidate):
                    break
                m = _LABEL_VALUE_RE.match(candidate)
                if not m:
                    break
                run_pairs.append((m.group(1).strip(), m.group(2).strip()))
                j += 1
            if run_pairs:
                blocks.append(ContentPayload(pairs=tuple(run_pairs), contiguous=True))
                spans.append((i, j))
                i = j
                continue

        i += 1

    return Segmentation(blocks=tuple(blocks), lines=tuple(lines), spans=tuple(spans))


def _remaining_prose(lines: list[str], consumed: set[int]) -> str:
    """The draft with every structural block lifted out.

    Paragraph breaks are preserved where they survive the removal, then runs
    of blank lines are collapsed -- pulling a table out from between two
    paragraphs otherwise leaves a three-line gap where it stood.
    """
    kept = ["" if idx in consumed else line for idx, line in enumerate(lines)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def segment_blocks(text: str) -> list[ContentPayload]:
    """Blocks only, for callers that do not need the prose remainder."""
    return list(segment_draft(text).blocks)


def deterministic_format(
    react_draft: str | None,
    cid: str | None = None,
    multi_section: bool | None = None,
    budget: RenderBudget | None = None,
) -> dict[str, Any]:
    """Structure react_draft into an AnswerCard-shaped dict with regex only.
    No LLM call. Returns {mode, direct_answer, sections}.

    The envelope choice is delegated to classify_envelope -- see that module
    for the ladder, and for why bullets outrank stats on this path.

    multi_section (default: MULTI_SECTION_ENABLED, currently off) switches
    from "find every shape, render the winner" to "segment the draft, render
    each block under the render budget". The single-section behavior is what
    ships today and what the existing tests pin; the multi-section path is
    the fix for long answers that carry more than one shape.
    """
    text = (react_draft or "").strip()
    if not text:
        return {"mode": "FACTUAL", "direct_answer": "", "sections": []}

    bolded = bold_key_facts(text)
    # A caller-supplied budget carries v2's grounding verdict (see
    # app.responder.v2_adapter). Without one we fall back to the local
    # raw-excerpt check, which is all this path can know on its own.
    if budget is None:
        budget = RenderBudget(is_raw_excerpt=_looks_like_raw_excerpt(text))
    elif _looks_like_raw_excerpt(text) and not budget.is_raw_excerpt:
        budget = replace(budget, is_raw_excerpt=True)

    if multi_section is None:
        multi_section = MULTI_SECTION_ENABLED

    if not multi_section:
        payload = extract_payload(text)
        verdict = classify_envelope(payload, budget=budget)
        log_verdict(verdict, cid=cid, payload=payload)
        section = build_section(payload, verdict)
        card = {
            "mode": "FACTUAL",
            "direct_answer": bolded,
            "sections": [section] if section else [],
        }
        _attach_presentation(card, [verdict])
        return card

    segmentation = segment_draft(text)
    sections: list[dict[str, Any]] = []
    verdicts: list = []
    rendered_blocks: list[int] = []
    for index, block in enumerate(segmentation.blocks):
        verdict = classify_envelope(block, budget=budget)
        verdicts.append(verdict)
        log_verdict(verdict, cid=cid, section_index=index, payload=block)
        section = build_section(block, verdict)
        if section:
            sections.append(section)
            rendered_blocks.append(index)
    if not segmentation.blocks:
        # No block at all is itself a verdict -- prose with no structure in it.
        verdicts.append(classify_envelope(ContentPayload(text=text), budget=budget))

    # Value-level duplicate suppression is skipped here on purpose. It
    # compares a section against direct_answer, and the split below has
    # already removed those lines wholesale -- running the value check as
    # well would only catch a fact the prose restates in its own words, which
    # is paraphrase, not duplication, and usually worth keeping.
    outcome = apply_render_budget(sections, direct_answer="")

    # Only blocks that actually rendered give up their lines. A block that
    # abstained keeps its content in the answer text -- it was declined a
    # card, not deleted.
    prose = segmentation.prose_excluding(rendered_blocks)

    card = {
        "mode": "FACTUAL",
        "direct_answer": _direct_answer_for(bolded, prose, outcome.sections),
        "sections": outcome.sections,
    }
    _attach_presentation(card, verdicts)
    return card


#: Why nothing was formatted, in words a surface can show. The contract's
#: "empty is not absent" rule applied to this surface: all three of these
#: previously rendered as silence, and "we declined to format this because
#: nothing grounds it" and "there was no structure here" are opposite
#: statements to a reader. The rule_id was in the trace and invisible in the
#: product.
#:
#: The TREATMENT is the frontend's -- thin evidence is this surface's version
#: of coverage's `unobservable` and should reuse whatever grey that gets,
#: rather than inventing a second visual language for the same state.
_ABSTAIN_NOTES: dict[str, str] = {
    "abstain.thin_evidence":
        "Shown as text, not as a card — nothing in the sources grounds this answer.",
    "abstain.raw_excerpt":
        "Shown as text, not as a card — this is quoted source material, not a checked summary.",
    "abstain.single_fact":
        "",
    "abstain.no_match":
        "",
}


def _attach_presentation(card: dict[str, Any], verdicts: list) -> None:
    """Record WHY the card looks the way it does, on the card.

    Additive: a frontend that does not read `presentation` is unaffected. But
    the data has to exist before any surface can show it, and it cannot be
    recovered later -- by the time the card is rendered the payload is gone.
    """
    if not verdicts:
        return
    abstained = [v for v in verdicts if v.abstained]
    if not abstained or any(not v.abstained for v in verdicts):
        # At least one section rendered: the card speaks for itself.
        return
    verdict = abstained[0]
    note = _ABSTAIN_NOTES.get(verdict.rule_id, "")
    presentation: dict[str, Any] = {
        "abstained": True,
        "rule_id": verdict.rule_id,
        "why": verdict.reason,
    }
    if note:
        presentation["note"] = note
    card["presentation"] = presentation


def _direct_answer_for(
    bolded: str,
    prose: str,
    sections: list[dict[str, Any]],
) -> str:
    """What the answer line says once the cards have taken their content.

    Three cases, and the third is the one that keeps this safe:

    1. Nothing rendered as a section -- the draft is the answer, unchanged.
    2. Sections rendered AND prose survives between them -- the prose is the
       answer. This is the fix: the reader stops seeing the same table rows,
       steps and phone numbers twice.
    3. Sections rendered and NOTHING survives -- the whole draft was one
       structured block. Fall back to the full draft rather than emitting an
       empty answer: direct_answer is the STREAMED anchor, so blanking it
       means the user watches an empty bubble until the card lands. Mild
       duplication beats a turn that looks broken while it loads.
    """
    if not sections:
        return bolded
    remaining = (prose or "").strip()
    if not remaining:
        return bolded
    return bold_key_facts(remaining)
