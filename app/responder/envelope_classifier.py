"""The envelope classifier — one pure function that decides presentation.

Per ENVELOPE_CLASSIFIER_SPEC.md (docs/ENVELOPE_CLASSIFIER_SPEC.md), Phase 1.

Before this module, three independent implementations decided which envelope
a section renders as: the regex fast path (deterministic_format.py), the
enricher prompt (chat_config.py:189-195), and the tool-hint builder
(final.py:build_pre_built_sections). They disagreed on their thresholds, so
identical content rendered differently depending on which path served the
turn. Users read that as random formatting; it was a reproducible fork.

The split this module enforces:

    extraction  -- prose or tool output -> ContentPayload (typed content)
    classification -- ContentPayload -> Verdict (which envelope, and why)
    rendering   -- Verdict + payload -> the AnswerCard section dict

Extraction stays with whoever owns the input (deterministic_format.py owns
prose; the tool owns its section_hint). Classification lives here and only
here. `format` is never a thing a caller -- or a model -- chooses.

Purity: no I/O, no model calls, no config reads at call time. Given identical
inputs this returns an identical verdict across processes and runs, which is
what makes it unit-testable and what makes the rule_id in the trace
meaningful.

LADDER ORDER (first match wins). Rule ids are stable strings, not indexes --
inserting a rule must never renumber the others, because rule_id is what the
decision trace is queried by.

    intent.explicit     user asked for a named format
    hint.typed          a tool supplied a typed section_hint
    shape.table         a real table: pipe table, or records sharing >=2 keys
    shape.chart         ordinal/time axis + numeric values   (P1, flag-gated)
    shape.bars          items carry a weight
    shape.conditions    items carry condition + result
    shape.bullets       explicit bullet markers, >= BULLETS_MIN_ITEMS
    shape.steps         ordered items, >= STEPS_MIN_ITEMS
    shape.stats         <= STATS_MAX_ITEMS short pairs
    shape.table.pairs   pairs that overflow the stats caps
    abstain.no_match    nothing matched -- prose, deliberately

Two orderings in that ladder are load-bearing and were NOT free choices:

1. bullets > steps > stats preserves the fast path's existing precedence,
   pinned by three named tests in tests/test_deterministic_format.py
   (test_bullets_take_priority_over_label_value_pairs and friends). The
   rationale is confidence, not structural specificity: a draft with a real
   3-item bullet list AND a stray "Deadline: 180 days" line is a list whose
   prose happens to contain a colon, not a stats card. Phase 1 is
   behavior-preserving, so this order stands.

2. bars and conditions sit ABOVE bullets because weight and condition/result
   are unambiguous typed signals that prose extraction never produces. No
   prose-extracted payload can reach them, so their position cannot change
   fast-path behavior -- but a typed payload carrying weights must not fall
   through to bullets.

The spec's draft ladder put stats above bullets/steps. That was wrong against
the existing tests; this is the deviation, and it is deliberate.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from mobius_contracts.taxonomies.envelope_thresholds import (
    BULLETS_MAX_AVG_WORDS,
    BULLETS_MIN_ITEMS,
    CHART_MIN_POINTS,
    MAX_RICH_BLOCKS_PER_TURN,
    PAIRS_MAX_ITEMS,
    PAIRS_MIN_ITEMS,
    STATS_MAX_ITEMS,
    STATS_MAX_VALUE_CHARS,
    STEPS_MIN_ITEMS,
    TABLE_MAX_ROWS,
    TABLE_MIN_ROWS,
)

logger = logging.getLogger(__name__)

#: Envelopes the frontend can render (bubble.ts _renderSectionBody). A verdict
#: naming anything outside this set is a bug -- the section would render blank.
RENDERABLE_FORMATS: frozenset[str] = frozenset(
    {"table", "stats", "bars", "steps", "conditions", "bullets"}
)

#: P1 (R9). Reserved in the ladder so enabling it later cannot renumber or
#: reorder anything; off until the frontend renderer exists.
CHART_ENABLED: bool = False

#: Render every structural block of a draft as its own section, under the
#: render budget, instead of emitting only the single winning shape.
#:
#: ON as of 2026-09-12. Measured on long drafts before the flip: multi-shape
#: answers lost 2 of 3 shapes -- a numbered submission procedure and a contact
#: block, both explicitly structured by the author, kept only as prose. The
#: render budget is what makes this safe to default on: blocks past the cap
#: degrade to bullets rather than stacking cards indefinitely.
#:
#: Set False to fall back to single-section (emit only the winning shape).
MULTI_SECTION_ENABLED: bool = True


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Item:
    """One entry in a list-shaped section. Field names mirror the frontend's
    data.items shape exactly (bubble.ts) so a payload needs no translation
    layer on the way out."""
    label: str = ""
    value: str = ""
    note: str = ""
    weight: float | None = None
    condition: str = ""
    result: str = ""


@dataclass(frozen=True)
class TableData:
    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class SeriesData:
    """P1 (R9). Shape reserved with the ladder slot."""
    x_label: str = ""
    y_label: str = ""
    points: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class ContentPayload:
    """Typed content with no presentation in it.

    An extractor populates whichever fields it actually found. Fields are not
    mutually exclusive -- real drafts carry more than one shape, and resolving
    that collision is precisely the ladder's job, not the extractor's.

    `explicit_list` distinguishes "the author wrote bullet markers" from
    "these happen to be peer items". Only the former reaches shape.bullets;
    it is the signal that carries the bullets > stats precedence.
    """
    text: str = ""
    table: TableData | None = None
    pairs: tuple[tuple[str, str], ...] = ()
    items: tuple[Item, ...] = ()
    explicit_list: bool = False
    ordered: bool = False
    series: SeriesData | None = None
    #: True when the shape came from one contiguous run of lines rather than
    #: from matches scattered through prose. This is what separates real
    #: structured notes from a regex over-matching sentences that happen to
    #: contain a colon -- and it is the only evidence that distinguishes them,
    #: so the pairs ceiling is relaxed when it holds. See _pairs_in_band.
    contiguous: bool = False


@dataclass(frozen=True)
class IntentSignals:
    """What the INPUT told us, independent of what came back.

    The fast path has none of this today and passes the default. It is a
    separate argument rather than a payload field because it describes the
    question, not the answer -- and because rule 1 must be able to win
    without inspecting content at all.
    """
    explicit_format: str | None = None
    #: Typed section_hint format supplied by a tool (rule 2). Authoritative.
    typed_hint_format: str | None = None
    #: Planner question_intent. Reserved for R10; unused in Phase 1.
    question_intent: str | None = None


@dataclass(frozen=True)
class RenderBudget:
    """Context the model structurally cannot see: how much rich formatting
    this turn has already spent, and whether the content is trustworthy
    enough to format confidently at all."""
    max_rich_blocks: int = MAX_RICH_BLOCKS_PER_TURN
    rich_blocks_used: int = 0
    #: Gate inputs (R3).
    is_raw_excerpt: bool = False
    is_thin_evidence: bool = False


@dataclass(frozen=True)
class Verdict:
    """format=None means abstain: render as prose, deliberately, not as a
    consolation bullets list."""
    format: str | None
    rule_id: str
    reason: str

    @property
    def abstained(self) -> bool:
        return self.format is None


# --------------------------------------------------------------------------
# Rule 1 -- explicit format request from the user's own words
# --------------------------------------------------------------------------

# Ordered most-specific first: "in bullet points" must not be swallowed by a
# looser "list" pattern. Anchored on the preposition so that a question ABOUT
# tables ("what does the fee table say") does not read as a request FOR one.
_EXPLICIT_FORMAT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:as|in|into)\s+(?:a\s+)?bullet(?:\s+point)?s?\b", re.I), "bullets"),
    (re.compile(r"\b(?:as|in|into)\s+(?:a\s+)?tables?\b", re.I), "table"),
    (re.compile(r"\b(?:as|in|into)\s+(?:a\s+)?(?:numbered\s+)?steps?\b", re.I), "steps"),
    (re.compile(r"\b(?:as|in|into)\s+(?:a\s+)?(?:bulleted\s+)?lists?\b", re.I), "bullets"),
    (re.compile(r"\btabulat(?:e|ed)\b", re.I), "table"),
)


def detect_explicit_format(user_message: str | None) -> str | None:
    """Rule 1's predicate, exposed for callers that build IntentSignals.

    This was previously an instruction in the enricher prompt
    (chat_config.py:196-201) asking the model to notice the request and honour
    it. Asking a model to run a regex is how the behavior became unreliable;
    it is a regex.
    """
    text = (user_message or "").strip()
    if not text:
        return None
    for pattern, fmt in _EXPLICIT_FORMAT_PATTERNS:
        if pattern.search(text):
            return fmt
    return None


# --------------------------------------------------------------------------
# Shape predicates (rules 3-10)
# --------------------------------------------------------------------------

def _has_table(payload: ContentPayload) -> bool:
    t = payload.table
    return bool(t and t.headers and len(t.rows) >= TABLE_MIN_ROWS)


def _has_chart(payload: ContentPayload) -> bool:
    s = payload.series
    return bool(s and len(s.points) >= CHART_MIN_POINTS)


def _has_bars(payload: ContentPayload) -> bool:
    return any(i.weight is not None for i in payload.items)


def _has_conditions(payload: ContentPayload) -> bool:
    return any(i.condition and i.result for i in payload.items)


def _avg_words(items: tuple[Item, ...]) -> float:
    labels = [i.label for i in items if i.label]
    if not labels:
        return 0.0
    return sum(len(l.split()) for l in labels) / len(labels)


def _is_list(payload: ContentPayload) -> bool:
    """Enough marked-up items to be a list at all. Length is checked
    separately so the ladder can tell "not a list" apart from "a list whose
    items are paragraphs" -- two different verdicts with two different
    honest renderings."""
    return payload.explicit_list and len(payload.items) >= BULLETS_MIN_ITEMS


def _has_bullets(payload: ContentPayload) -> bool:
    return _is_list(payload) and _avg_words(payload.items) <= BULLETS_MAX_AVG_WORDS


def _has_steps(payload: ContentPayload) -> bool:
    return payload.ordered and len(payload.items) >= STEPS_MIN_ITEMS


def _pairs_in_band(payload: ContentPayload) -> bool:
    """The upper bound guards against the label:value regex over-matching
    prose sentences that contain a colon -- seven scattered matches across a
    long narrative is almost always that, not structure.

    It is NOT a statement that seven real facts are too many. A contiguous run
    of label:value lines is structure by construction, so the ceiling lifts to
    the table row cap. Without this, a nine-line block of genuine appeal
    deadlines abstains to prose (found stress-testing long drafts, 2026-09-12)
    -- the exact under-formatting the classifier exists to prevent.
    """
    count = len(payload.pairs)
    if count < PAIRS_MIN_ITEMS:
        return False
    if payload.contiguous:
        return count <= TABLE_MAX_ROWS
    return count <= PAIRS_MAX_ITEMS


def _has_stats(payload: ContentPayload) -> bool:
    """Stats tiles need BOTH a short item count and short values -- a tile is
    a value, not a sentence. Either cap exceeded routes to a table instead,
    which is rule shape.table.pairs, not a fallthrough to prose."""
    if not _pairs_in_band(payload):
        return False
    if len(payload.pairs) > STATS_MAX_ITEMS:
        return False
    return all(len(v) <= STATS_MAX_VALUE_CHARS for _, v in payload.pairs)


# --------------------------------------------------------------------------
# Gates (R3) -- abstain is a verdict, not a fallthrough
# --------------------------------------------------------------------------

_RAW_EXCERPT_RE = re.compile(r"^\[\d+\]\s")


def looks_like_raw_excerpt(text: str | None) -> bool:
    """react's thin-evidence hedge ships a retrieved chunk verbatim rather
    than synthesized prose, deliberately, so the answer does not look more
    confident than the evidence supports. Real policy text inside that chunk
    often has genuine "Label: Value" lines; promoting them into a "Key Facts"
    card would undo the hedge and present an unvetted excerpt as something we
    structured with confidence (2026-08-10, ReAct agent's real-sample audit).
    """
    return bool(_RAW_EXCERPT_RE.match((text or "").strip()))


def _gate(payload: ContentPayload, budget: RenderBudget) -> Verdict | None:
    """Returns an abstain verdict, or None to let the ladder run."""
    if budget.is_raw_excerpt or looks_like_raw_excerpt(payload.text):
        return Verdict(None, "abstain.raw_excerpt",
                       "verbatim retrieved excerpt; structuring it would overstate confidence")
    if budget.is_thin_evidence:
        return Verdict(None, "abstain.thin_evidence",
                       "no supporting sources; a card would imply more certainty than we have")
    if len(payload.pairs) == 1 and not payload.items and payload.table is None:
        return Verdict(None, "abstain.single_fact",
                       "one label/value pair is not a pattern; a card for it is noise")
    return None


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------

def classify_envelope(
    payload: ContentPayload,
    intent: IntentSignals | None = None,
    budget: RenderBudget | None = None,
) -> Verdict:
    """Decide the presentation envelope for one section's content.

    Pure. The only function in the codebase permitted to choose a `format`.
    """
    intent = intent or IntentSignals()
    budget = budget or RenderBudget()

    # Rule 1 -- the user named a format. Beats every shape rule and every
    # gate: if someone asked for a table, a thin-evidence hedge is a reason to
    # say so in prose alongside, not a reason to silently ignore the request.
    if intent.explicit_format in RENDERABLE_FORMATS:
        return Verdict(intent.explicit_format, "intent.explicit",
                       "user asked for this format in their message")

    # Rule 2 -- a tool supplied a typed hint. Never re-classified: the tool
    # knows the shape of its own output better than any predicate over its
    # rendered text can. Custom formats (appeals_playbook, appeals_rules) are
    # deliberately allowed through unchecked -- the frontend owns their data
    # blob, so RENDERABLE_FORMATS does not apply to them.
    if intent.typed_hint_format:
        return Verdict(intent.typed_hint_format, "hint.typed",
                       "typed section_hint supplied by the tool")

    gated = _gate(payload, budget)
    if gated is not None:
        return gated

    if _has_table(payload):
        return Verdict("table", "shape.table", "rows with consistent columns")
    if CHART_ENABLED and _has_chart(payload):
        return Verdict("chart", "shape.chart", "numeric values over an ordinal axis")
    if _has_bars(payload):
        return Verdict("bars", "shape.bars", "items carry a weight")
    if _has_conditions(payload):
        return Verdict("conditions", "shape.conditions", "items carry condition and result")
    if _has_bullets(payload):
        return Verdict("bullets", "shape.bullets", "explicit list of peer items")
    if _is_list(payload):
        # Marked up as a list, but the items are paragraph-length. Bulleting
        # paragraphs makes them look scannable when they are not, and the
        # marks add nothing a reader can use. Named rather than falling
        # through to abstain.no_match so the trace says which it was --
        # "there was no structure here" and "the structure was too heavy to
        # bullet" are different findings, and only the second one is a
        # signal that the ANSWER, not the formatter, should change.
        return Verdict(None, "abstain.prose_list",
                       f"list items average {_avg_words(payload.items):.0f} words; "
                       "these are paragraphs, not bullets")
    if _has_steps(payload):
        return Verdict("steps", "shape.steps", "ordered items; sequence carries meaning")
    if _has_stats(payload):
        return Verdict("stats", "shape.stats", "few pairs with short values")
    if _pairs_in_band(payload):
        return Verdict("table", "shape.table.pairs",
                       "pairs exceed the stats caps; a table holds them without truncation")

    return Verdict(None, "abstain.no_match", "no confident structure; prose is the honest rendering")


# --------------------------------------------------------------------------
# Verdict -> section dict
# --------------------------------------------------------------------------

def build_section(
    payload: ContentPayload,
    verdict: Verdict,
    label: str | None = None,
    intent: str = "process",
) -> dict[str, Any] | None:
    """Render a verdict into an AnswerCard section dict.

    Field placement matches what bubble.ts actually reads: bullets live at the
    top level as sec["bullets"], everything else under sec["data"]. Getting
    this wrong renders an empty section rather than failing loudly, which is
    why it lives in one place.

    Every format is built through the coercion views below rather than from
    one privileged field. That is what makes rule 1 real: a user who asks for
    bullets over table-shaped content must GET bullets. Reading only
    payload.items for a bullets verdict silently returned None instead --
    the verdict said bullets, the card showed nothing (found 2026-09-12
    while demonstrating the intent override). For shape-derived verdicts the
    views resolve to the same field the shape rule matched on, so this
    changes nothing about how the ladder renders.
    """
    if verdict.abstained or verdict.format is None:
        return None

    fmt = verdict.format
    section: dict[str, Any] = {
        "intent": intent,
        "label": label or _DEFAULT_LABELS.get(fmt, "Details"),
        "format": fmt,
    }

    if fmt == "bullets":
        bullets = _as_labels(payload)
        if not bullets:
            return None
        section["bullets"] = bullets
        return section

    if fmt == "table":
        data = _as_table(payload)
        if data is None:
            return None
        section["data"] = data
        return section

    if fmt == "stats":
        pairs = _as_pairs(payload)
        if not pairs:
            return None
        section["data"] = {"items": [{"label": l, "value": v} for l, v in pairs]}
        return section

    if fmt == "steps":
        labels = _as_labels(payload)
        if not labels:
            return None
        section["data"] = {"items": [{"label": s} for s in labels]}
        return section

    if fmt in ("bars", "conditions"):
        # Not coercible: a weight and an if/then pairing cannot be invented
        # from content that does not carry them. Rule 1 never asks for these
        # -- detect_explicit_format only ever names table, bullets or steps --
        # so this stays a typed-payload path.
        items = [d for d in (_item_dict(i) for i in payload.items) if d]
        if not items:
            return None
        section["data"] = {"items": items}
        return section

    return None


_DEFAULT_LABELS: dict[str, str] = {
    "table": "Details",
    "stats": "Key Facts",
    "bullets": "Key Points",
    "steps": "Steps",
    "bars": "Breakdown",
    "conditions": "Conditions",
}


# --- coercion views -------------------------------------------------------
# One payload, read three ways. Each view prefers the field a shape rule would
# have matched on, then falls back through the others, so a format the content
# did not suggest can still be built from what the content actually has.

#: A label/value pair is two columns. Structural arity, not a tunable bound --
#: named so the no-hardcoded-thresholds guard can tell the difference.
_PAIR_COLUMNS: int = 2


def _as_labels(payload: ContentPayload) -> list[str]:
    """Flat lines -- for bullets and steps."""
    if payload.items:
        return [i.label for i in payload.items if i.label]
    if payload.pairs:
        return [f"{l}: {v}" for l, v in payload.pairs]
    if payload.table and payload.table.rows:
        return [" — ".join(c for c in row if c) for row in payload.table.rows]
    return []


def _as_pairs(payload: ContentPayload) -> list[tuple[str, str]]:
    """Label/value pairs -- for stats."""
    if payload.pairs:
        return list(payload.pairs)
    if payload.items:
        return [(i.label, i.value or i.result) for i in payload.items if i.label and (i.value or i.result)]
    if payload.table and len(payload.table.headers) == _PAIR_COLUMNS:
        return [(row[0], row[1]) for row in payload.table.rows if len(row) >= _PAIR_COLUMNS]
    return []


def _as_table(payload: ContentPayload) -> dict[str, Any] | None:
    """Headers + rows. A table verdict reaches here from a real table
    (shape.table), from pairs that overflowed the stats caps
    (shape.table.pairs), or from a user who asked for one over list-shaped
    content (intent.explicit)."""
    if payload.table and payload.table.headers and payload.table.rows:
        rows = [list(r) for r in payload.table.rows]
        data: dict[str, Any] = {"headers": list(payload.table.headers)}
        if len(rows) > TABLE_MAX_ROWS:
            data["rows"] = rows[:TABLE_MAX_ROWS]
            data["truncated"] = len(rows) - TABLE_MAX_ROWS
        else:
            data["rows"] = rows
        return data
    if payload.pairs:
        return {"headers": ["Item", "Detail"], "rows": [[l, v] for l, v in payload.pairs]}
    if payload.items:
        valued = [i for i in payload.items if i.value or i.result]
        if valued:
            return {"headers": ["Item", "Detail"],
                    "rows": [[i.label, i.value or i.result] for i in valued]}
        labels = [i.label for i in payload.items if i.label]
        if labels:
            return {"headers": ["Item"], "rows": [[l] for l in labels]}
    return None


def _item_dict(item: Item) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("label", "value", "note", "condition", "result"):
        val = getattr(item, key)
        if val:
            out[key] = val
    if item.weight is not None:
        out["weight"] = item.weight
    return out


# --------------------------------------------------------------------------
# R6 -- render budget
# --------------------------------------------------------------------------

_RICH_FORMATS: frozenset[str] = frozenset({"table", "stats", "bars", "conditions", "steps", "chart"})


@dataclass
class BudgetOutcome:
    sections: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def apply_render_budget(
    sections: list[dict[str, Any]],
    direct_answer: str = "",
    max_rich_blocks: int = MAX_RICH_BLOCKS_PER_TURN,
    protected_labels: set[str] | None = None,
) -> BudgetOutcome:
    """Cap the rich blocks in one turn and drop sections the prose already said.

    Two failure modes this closes, both of which were previously addressed by
    asking the model nicely (chat_config.py:184-188, "DO NOT RESTATE CARD DATA
    IN PROSE"):

    1. Every answer becoming a wall of cards. Sections past the cap degrade to
       bullets -- the content survives, the visual weight does not.
    2. The same facts rendered twice, once in direct_answer and once in a
       card. The card is the surface for those; the duplicate goes.

    Tool-originated sections (protected_labels) are never the ones degraded --
    a typed hint outranks a section the pipeline inferred, so the cap is spent
    on hints first and inferred sections absorb the degradation.
    """
    protected = protected_labels or set()
    outcome = BudgetOutcome()
    rich_used = 0

    ordered = sorted(
        sections,
        key=lambda s: 0 if (s.get("label") or "") in protected else 1,
    )

    for sec in ordered:
        fmt = sec.get("format") or ""
        label = sec.get("label") or ""

        if _duplicates_prose(sec, direct_answer):
            outcome.dropped.append(label)
            continue

        if fmt in _RICH_FORMATS:
            if rich_used >= max_rich_blocks and label not in protected:
                degraded = _degrade_to_bullets(sec)
                if degraded is not None:
                    outcome.sections.append(degraded)
                    outcome.degraded.append(label)
                continue
            rich_used += 1

        outcome.sections.append(sec)

    return outcome


def _duplicates_prose(section: dict[str, Any], direct_answer: str) -> bool:
    """True when every value the section would display already appears in the
    prose. Conservative by construction: one novel cell is enough to keep the
    section, because a card that adds anything is worth its space."""
    if not direct_answer:
        return False
    values = _section_values(section)
    if not values:
        return False
    haystack = direct_answer.lower()
    return all(v.lower() in haystack for v in values)


def _section_values(section: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for b in section.get("bullets") or []:
        if isinstance(b, str) and b.strip():
            out.append(b.strip())
    data = section.get("data")
    if isinstance(data, dict):
        for row in data.get("rows") or []:
            out.extend(str(c).strip() for c in row if str(c).strip())
        # Item labels are excluded deliberately: a label is the card's own
        # framing ("Filing"), not a fact the prose could have restated. The
        # duplication that matters is the fact -- the day count, the dollar
        # figure, the deadline -- so only value-bearing fields count.
        for item in data.get("items") or []:
            if isinstance(item, dict):
                for key in ("value", "result"):
                    val = item.get(key)
                    if isinstance(val, str) and val.strip():
                        out.append(val.strip())
    return out


def _degrade_to_bullets(section: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten a rich section into bullets, keeping its content and losing
    only its visual weight."""
    bullets: list[str] = []
    data = section.get("data")
    if isinstance(data, dict):
        for row in data.get("rows") or []:
            cells = [str(c).strip() for c in row if str(c).strip()]
            if cells:
                bullets.append(": ".join(cells[:2]) if len(cells) > 1 else cells[0])
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or item.get("result") or "").strip()
            if label and value:
                bullets.append(f"{label}: {value}")
            elif label:
                bullets.append(label)
    if not bullets:
        return None
    return {
        "intent": section.get("intent") or "process",
        "label": section.get("label") or "Key Points",
        "format": "bullets",
        "bullets": bullets,
    }


# --------------------------------------------------------------------------
# R7 -- decision trace
# --------------------------------------------------------------------------

def log_verdict(
    verdict: Verdict,
    cid: str | None = None,
    section_index: int = 0,
    payload: ContentPayload | None = None,
    degraded: bool = False,
) -> None:
    """One structured line per section, queryable by rule_id.

    This is what turns "why was this a table?" from an investigation into a
    grep. Every verdict is traced, abstains included -- a turn that rendered
    as prose is a decision, and the trace has to show which gate made it.
    """
    logger.info(
        "[envelope] cid=%s section=%d rule_id=%s format=%s shape=%s degraded=%s reason=%s",
        cid or "-",
        section_index,
        verdict.rule_id,
        verdict.format or "prose",
        _shape_summary(payload) if payload else "-",
        degraded,
        verdict.reason,
    )


def _shape_summary(payload: ContentPayload) -> str:
    parts = []
    if payload.table:
        parts.append(f"table={len(payload.table.rows)}r")
    if payload.pairs:
        parts.append(f"pairs={len(payload.pairs)}")
    if payload.items:
        parts.append(f"items={len(payload.items)}")
    if payload.explicit_list:
        parts.append("list")
    if payload.ordered:
        parts.append("ordered")
    if payload.series:
        parts.append(f"series={len(payload.series.points)}")
    return ",".join(parts) or "prose"
