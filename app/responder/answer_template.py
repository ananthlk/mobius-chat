"""A SUGGESTED answer shape, computed before react writes — no model.

Ananth, 2026-09-12: "based on what you have seen in the question, the summary,
the sources, start to write a template suggestion for the llm to follow as a
suggestion".

WHY THIS EXISTS. The formatter downstream can only detect, rearrange, or
refuse. It cannot turn three paragraphs into a comparison, because that is
writing. Meanwhile everything needed to know the answer SHOULD be a comparison
is available before a word is written: the question names three payers, the
planner split it into three sub-questions, and retrieval came back with a
different number of pages for each. The decision was always deterministic. The
only part that needs a model is the prose, and a model writes prose well when
it is told what shape to write it in.

So the determinism moves up. This proposes the shape; react writes it.

REACT'S OWN FORMAT RULES ARE THE BASELINE THIS OVERRIDES, and only for the
turns where they are wrong. REACT_FORMAT_RULES_TEXT (react/prompts.py:447)
hard-codes one shape -- a bold line plus "2-4 short bullet points (each 10-25
words)" -- and never mentions a table. A three-entity comparison is therefore
structurally unable to come back as one, which is the defect behind the live
three-payer answer: bullets of 61, 64 and 69 words against react's own 25-word
cap, forced into a list because no instruction permitted anything else.

A SUGGESTION, NOT A MANDATE, deliberately. The evidence can contradict the
question's shape -- a comparison where two of three entities returned nothing
is not a table, it is a finding -- and react is the only thing here that has
read the evidence. The wording says so. What this removes is not react's
judgement; it is react having no option to exercise it on.

RENDERS ONLY WHEN ITS FACT EXISTS, matching v2/blocks.py's design rule. No
signal, no block: a template suggested from nothing is noise in a prompt that
is already long, and an empty section is a claim that there is nothing there.

PURE. Signals in, text out. No I/O, no ctx, no model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from mobius_contracts.taxonomies.envelope_thresholds import BULLETS_MAX_AVG_WORDS


@dataclass(frozen=True)
class QuestionShape:
    """What the QUESTION determined, before any answer existed.

    `entities` comes from the planner, which already splits "compare X, Y and
    Z" into one sub-question per entity -- a count, not a judgement. The
    fallback parser below is for callers without a planner result; it is
    strictly worse and says so.
    """
    entities: tuple[str, ...] = ()
    #: The noun phrase the question asks ABOUT ("care management philosophy").
    #: Becomes the comparison column header, verbatim -- the user's own words,
    #: never a paraphrase.
    axis: str = ""
    #: A format the user named outright ("as a table"). Outranks everything.
    explicit_format: str | None = None
    #: Planner question_intent, when available.
    intent: str = ""
    #: The question AS ASKED. Added because _is_procedural was testing `axis`
    #: — a noun phrase extracted for a table header — with a regex written to
    #: match a question, so the procedural branch was unreachable. The shape of
    #: a question ("how do I…") lives in the question, not in a header.
    text: str = ""


@dataclass(frozen=True)
class EvidenceShape:
    """What RETRIEVAL came back with, per entity where the fan-out allows it.

    `pages_per_entity` is the honest measure of depth: two citations to the
    same page is one page. The live three-payer answer cited UnitedHealthcare
    twice and both were p5, which reads as two sources until you compare the
    numbers.
    """
    pages_per_entity: dict[str, int] = field(default_factory=dict)
    documents: int = 0
    total_chunks: int = 0

    def entities_with_nothing(self, entities: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(e for e in entities if not self.pages_per_entity.get(e))

    @property
    def skewed(self) -> bool:
        """One entity resting on materially less than another.

        Not a quality judgement -- a statement that rows of equal visual
        weight would misrepresent unequal evidence.
        """
        counts = [n for n in self.pages_per_entity.values() if n]
        if len(counts) < 2:
            return False
        return max(counts) >= 2 * min(counts)


# --- axis extraction -------------------------------------------------------
# The column header is lifted from the question verbatim. No paraphrase, and
# no header at all when the patterns do not match -- "Answer" is a worse
# header than one the reader wrote themselves, but inventing a topic name is
# worse than both.

_AXIS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bwhat\s+(?:is|are)\s+(?:the\s+)?(.+?)\s+(?:for|of|at|in)\b", re.I),
    re.compile(r"\bcompare\s+(?:the\s+)?(.+?)\s+(?:for|of|across|between)\b", re.I),
    re.compile(r"\b(?:tell me|show me|list)\s+(?:the\s+)?(.+?)\s+(?:for|of)\b", re.I),
    # Bare noun-phrase queries, which is most of live traffic: "timely filing
    # deadline for Sunshine Health and Aetna". No interrogative to anchor on,
    # so anchor on the preposition and take what precedes it. Last because it
    # is the loosest -- the patterns above carry an interrogative and are
    # therefore surer of what they matched.
    re.compile(r"^(.+?)\s+(?:for|of|across|between)\b", re.I),
)

_STOP_TAIL = re.compile(r"\s+(?:for|of|at|in|across|between)\s*$", re.I)


def extract_axis(question: str) -> str:
    """The noun phrase the question asks about, or "" when unsure.

    Returning "" is a real answer -- the caller falls back to a generic header
    rather than the parser guessing a topic and putting words in the reader's
    mouth.
    """
    text = (question or "").strip()
    if not text:
        return ""
    for pattern in _AXIS_PATTERNS:
        m = pattern.search(text)
        if m:
            axis = _STOP_TAIL.sub("", m.group(1).strip(" ?.,"))
            # A whole clause is not a column header. Four words is the width
            # a header can carry before it wraps and stops being scannable.
            if axis and len(axis.split()) <= 6:
                return axis
    return ""


# --- entity recovery from the planner ------------------------------------

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*(?:'s)?")


def _norm(word: str) -> str:
    """Casefold and drop a possessive, so "Molina's" and "molina" are one
    token. Nothing else -- no stemming, no synonyms."""
    return word.lower().rstrip("'s") if word.lower().endswith("'s") else word.lower()


def entities_from_subquestions(subquestions: tuple[str, ...]) -> tuple[str, ...]:
    """The entity each sub-question is ABOUT, by set difference.

    The planner already splits "compare X, Y and Z" into one sub-question per
    entity, and those sub-questions are near-identical by construction --
    "What is Molina's care management program?" beside "What is Sunshine
    Health's care management program?". So the entity is simply the tokens
    present in one and absent from every other. No list of what an entity
    looks like, no capitalisation rule, no conjunction parsing: set
    arithmetic over the planner's own output.

    This REPLACES a surface parser that read a conjunction list out of the
    question text. Running that parser over live traffic found three defects
    in four minutes -- it took a whole question as a column header, it read
    "providers and members" as two entities to tabulate, and an Oxford comma
    produced an entity named "and UnitedHealthcare". Governor's ruling,
    2026-09-13: a fallback exercised only when the good path fails, and known
    buggy, makes the failure worse and hides it. Delete rather than deprecate.

    Returns () when the sub-questions do not differ in the way a per-entity
    split produces -- which is a real answer meaning "this was not a
    comparison", not a failure to parse.
    """
    texts = [t.strip() for t in subquestions if t and t.strip()]
    if len(texts) < 2:
        return ()

    tokenised = [_WORD.findall(t) for t in texts]
    normed = [{_norm(w) for w in toks} for toks in tokenised]

    out: list[str] = []
    for i, toks in enumerate(tokenised):
        others: set[str] = set()
        for j, s in enumerate(normed):
            if j != i:
                others |= s
        unique = [w for w in toks if _norm(w) not in others]
        if not unique:
            # One sub-question says nothing the others do not. That is not a
            # per-entity split, so there are no entities to name here.
            return ()
        out.append(" ".join(w.rstrip("'s") if w.lower().endswith("'s") else w
                            for w in unique))
    return tuple(out)


# --- the proposer ----------------------------------------------------------

_PREAMBLE = (
    "SUGGESTED ANSWER SHAPE — computed from the question and what retrieval "
    "returned, before you wrote anything. It REPLACES the default bullet shape "
    "in the FORMAT RULES for this turn only.\n"
    "This is a suggestion. You have read the evidence and this has not: if the "
    "evidence contradicts the shape below, follow the evidence and say why in "
    "one line.\n"
)


def suggest_template(
    question: QuestionShape,
    evidence: EvidenceShape | None = None,
) -> str | None:
    """The suggested shape, or None when no signal supports one.

    Ladder, first match wins -- the same discipline as the envelope
    classifier, one stage earlier. Rule ids are the reasons, and they are
    worth logging: a turn where no rule fired is a turn whose shape nobody
    decided, which is the state the product is in today on every question.
    """
    evidence = evidence or EvidenceShape()

    if question.explicit_format:
        return _explicit(question)
    if len(question.entities) >= 2:
        return _comparison(question, evidence)
    if _is_procedural(question):
        return _procedure(question)
    return None


def _explicit(question: QuestionShape) -> str:
    """The user named a format. Nothing outranks that, including evidence --
    a request you decline is a request you have to answer for."""
    fmt = question.explicit_format
    shapes = {
        "table": "a markdown pipe table",
        "bullets": "a bulleted list",
        "steps": "a numbered list of steps",
    }
    return (
        _PREAMBLE
        + f"\nThe user asked for {shapes.get(fmt, fmt)} in their own words. "
        f"Return the \"answer\" field as {shapes.get(fmt, fmt)}, even if the "
        "content's natural shape suggests otherwise. If the evidence cannot "
        "fill that shape, say so in one line and give what you have.\n"
    )


def _comparison(question: QuestionShape, evidence: EvidenceShape) -> str:
    """Two or more entities named: the answer is a comparison, and a
    comparison written as prose makes the reader do the comparing."""
    entities = question.entities
    axis = question.axis or "Answer"
    header = axis[:1].upper() + axis[1:] if axis else "Answer"

    lines = [
        _PREAMBLE,
        f"\nThe question was split into {len(entities)} parts, so the answer is "
        "a comparison. Return the \"answer\" field as a markdown pipe table "
        "with ONE ROW PER PART and no prose above it:\n",
        # BLANK corner cell, deliberately. The planner splits on whatever the
        # question varies -- three payers, or two topics for one payer, and
        # entities_from_subquestions recovers both the same way. "Entity"
        # would be wrong for the second kind and "Topic" for the first, and
        # the rows label themselves either way. A conventional comparison
        # table has an empty top-left.
        f"|  | {header} | Source |",
        "| --- | --- | --- |",
    ]
    for e in entities:
        lines.append(f"| {e} | … | … |")

    lines.append(
        f"\nKeep each cell to a phrase, not a sentence — a cell over "
        f"{BULLETS_MAX_AVG_WORDS} words stops being scannable and the table "
        "stops earning its place. Put the detail that does not fit in one "
        "short paragraph BELOW the table."
    )

    missing = evidence.entities_with_nothing(entities)
    if missing:
        lines.append(
            "\nRetrieval returned nothing for: "
            + ", ".join(missing)
            + ". Keep the row and say so in the cell. A silently missing row "
            "reads as something you chose not to mention."
        )

    if evidence.skewed:
        depth = ", ".join(
            f"{e} {evidence.pages_per_entity.get(e, 0)} page(s)" for e in entities
        )
        lines.append(
            f"\nEvidence is uneven ({depth}). Rows of equal weight would "
            "present unequal evidence as equal — note the difference in one "
            "line under the table."
        )

    lines.append(
        "\nIf the parts turn out not to be comparable on this axis — "
        "different things were retrieved for each — say that instead of "
        "forcing parallel rows. That is a finding, not a failure."
    )
    return "\n".join(lines) + "\n"


_PROCEDURAL = re.compile(
    # Widened 2026-09-15 with the questions it was measured missing.
    # "what steps must a provider take" is as procedural as "how do i", and the
    # original required "the steps" so it matched neither that nor "steps to".
    r"\b(?:how\s+do\s+(?:i|you|we)|how\s+can\s+i|how\s+to"
    r"|what\s+steps|what\s+(?:are\s+)?the\s+steps|steps\s+to"
    r"|process\s+for|walk\s+me\s+through|procedure)\b",
    re.I,
)


def _is_procedural(question: QuestionShape) -> bool:
    """🔴 THIS READ THE AXIS, AND THE AXIS IS ALMOST ALWAYS EMPTY.

    _PROCEDURAL is written to match a QUESTION — "how do i", "how to", "what
    are the steps", "process for", "walk me through". It was applied to
    `question.axis`, which is a noun phrase extracted for use as a table column
    header and comes back "" for every procedural question I measured:

        "how do i appeal a CARC 22 denial for sunshine health?"
            axis = ''   procedural on axis = False   on QUESTION = True
        "how do i submit a corrected claim to Aetna?"
            axis = ''   procedural on axis = False   on QUESTION = True

    So `_procedure()` — the branch that asks for numbered steps — was
    UNREACHABLE from question text. Only `intent == "procedural"` could reach
    it, and nothing on this path sets intent. A pattern built for one field,
    applied to another, and dead in a way no test noticed because both sides
    were individually correct.

    That is why every appeal answer came back as bullets: not the model
    choosing badly, and not the classifier misreading it — the one instruction
    that would have asked for steps never rendered.
    """
    return (bool(_PROCEDURAL.search(question.text or ""))
            or bool(_PROCEDURAL.search(question.axis or ""))
            or question.intent == "procedural")


def _procedure(question: QuestionShape) -> str:
    return (
        _PREAMBLE
        + "\nThis asks how something is done. Return the \"answer\" field as a "
        "numbered list, one action per step, in the order they are performed:\n"
        "\nStep 1: …\nStep 2: …\n"
        "\nEach step is an instruction, so it may run longer than a bullet. "
        "Put deadlines, contacts and form names inside the step they belong "
        "to, not in a separate list.\n"
    )


def suggest_from_question(
    question_text: str,
    entities: tuple[str, ...] = (),
    evidence: EvidenceShape | None = None,
    intent: str = "",
) -> str | None:
    """Convenience entry point. `entities` comes from the planner -- via
    entities_from_subquestions -- and is NOT derived from the question text.
    Nothing here parses entities out of prose any more."""
    from app.responder.envelope_classifier import detect_explicit_format

    return suggest_template(
        QuestionShape(
            text=question_text or "",
            entities=entities,
            # A column header is a noun phrase. The question text itself is a
            # worse header than a generic one, so extraction failing returns
            # "" and _comparison falls back to "Answer".
            axis=extract_axis(question_text),
            explicit_format=detect_explicit_format(question_text),
            intent=intent,
        ),
        evidence,
    )
