"""v2 integration output -> the formatter's inputs.

Per docs/v2-ux-contract.md (Governor seat, b2fcea4). The contract's own rule --
"a default you invent becomes a second author of the answer" -- is what this
module exists to honour on the formatting side.

THE SECOND-AUTHOR PROBLEM THIS REMOVES. The classifier's RenderBudget carries
`is_thin_evidence`: the gate that decides whether a turn is grounded enough to
format confidently. It was computed from a local heuristic in the responder --
while v2's integrator was, on the same turn, deciding exactly that question
properly, against the facts, and recording it in coverage[]. Two authors of one
judgement, free to disagree, with no way to tell which one the user saw. Now
the integrator decides and the formatter reads.

EMPTY IS NOT ABSENT (contract §4). Every function here distinguishes "v2 ran
and found nothing" from "v2 did not run". A turn with no v2_integration is not
a grounded turn and not an ungrounded one -- it is a turn where nobody checked,
and the formatter falls back to its own gates rather than assuming either. That
is why `grounding_from_v2` returns None rather than a default.

UNOBSERVABLE IS NOT A PASS (contract §2). It means the answer discusses a part
and nothing grounds it -- indistinguishable from an answer written from the
model's memory. It never counts toward grounding here, and the reason it
produced reaches the card so the surface can say so.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.responder.envelope_classifier import (
    ContentPayload,
    Item,
    RenderBudget,
    TableData,
)

logger = logging.getLogger(__name__)

#: coverage[].status values that mean a part IS grounded. Deliberately the
#: short list: `partial` is the critic's judgement rather than provenance, and
#: `unobservable` is the state the contract exists to stop reading as a pass.
GROUNDED_STATUSES: frozenset[str] = frozenset({"supported"})

#: Statuses that say the answer discusses something nothing backs. Named
#: separately from "not grounded" because `not_attempted` means nobody looked
#: -- a different thing to tell a user than "we looked and could not tell".
UNVERIFIED_STATUSES: frozenset[str] = frozenset({"unobservable", "not_attempted"})

#: Statuses that represent an actual VERDICT about a part, as opposed to an
#: admission that we could not reach one. `unobservable` is deliberately
#: absent: it is the contract's "we could not tell", and counting it as a
#: verdict is how could-not-check becomes checked-false. `not_attempted` is
#: also absent -- nobody looked, which is a fact about us, not about the part.
DECISIVE_STATUSES: frozenset[str] = frozenset({"supported", "partial", "unsupported"})


@dataclass(frozen=True)
class Grounding:
    """What v2 concluded about this turn's evidence, as the formatter needs it.

    `checked` is the empty-vs-absent flag and the reason this is a dataclass
    rather than a bare bool: False here means v2 ran and found nothing
    grounded, which is a finding. A turn where v2 never ran returns None from
    grounding_from_v2 and never constructs one of these.
    """
    checked: bool = True
    grounded_parts: int = 0
    unverified_parts: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()
    unsupported_claims: int = 0
    #: Did the check produce a usable answer, or only "could not tell"?
    #: See `conclusive` -- this is the field that stops us repeating the bug
    #: Governor is currently fixing on their own side.
    decisive_parts: int = 0
    critic_ran: bool = False

    @property
    def conclusive(self) -> bool:
        """Did the integrator manage to CHECK anything at all?

        An answer whose every part came back `unobservable` has not been
        found ungrounded -- it has not been examined. The contract is explicit
        that unobservable means "we could not tell", and Governor is presently
        fixing the mirror of this on their side (a critic given no facts was
        marking grounded claims "unsupported": could-not-check rendered as
        checked-false). Treating an all-unobservable turn as evidence of
        thinness would be the same defect one layer down.

        This matters TODAY, not theoretically: react still returns v1-shaped
        responses on most turns, so `facts` is empty, so every part comes back
        unobservable. Without this, the formatter suppresses every card on
        essentially all live traffic -- measured, not predicted.
        """
        return bool(self.decisive_parts or self.citations or self.critic_ran)

    @property
    def thin(self) -> bool:
        """Checked, conclusively, and nothing came back grounded.

        Three conditions, and all three are load-bearing:

        - CONCLUSIVE, so "we could not tell" never suppresses a card.
        - nothing grounded. Not "some part was unobservable" -- a three-payer
          answer with two payers grounded is not a thin turn, and suppressing
          its table would punish the answer for the part it got right.
        - no citations, because a citation is provenance regardless of what
          the critic concluded about it.

        Until the critic is reliable this will rarely fire, which is the
        correct failure direction: formatting an ungrounded answer is a
        smaller harm than silently stripping structure from a grounded one.
        """
        return self.conclusive and self.grounded_parts == 0 and not self.citations

    @property
    def inconclusive(self) -> bool:
        """v2 ran and could not tell us anything. Not grounded, not ungrounded.

        Worth surfacing -- it is the state a confident wrong answer produces --
        but never worth suppressing formatting over.
        """
        return not self.conclusive

    @property
    def partially_unverified(self) -> bool:
        return bool(self.unverified_parts) and not self.thin


def grounding_from_v2(v2_integration: dict[str, Any] | None) -> Grounding | None:
    """Read v2's coverage verdict. None means v2 did not run on this turn.

    None and Grounding(grounded_parts=0) are different answers and the caller
    must be able to tell them apart -- the first says nobody checked, the
    second says somebody checked and found nothing.
    """
    if not isinstance(v2_integration, dict) or not v2_integration:
        return None

    ran = v2_integration.get("ran")
    if isinstance(ran, dict) and ran.get("assemble") not in (None, "ok"):
        # assemble is the deterministic half; if it did not run there is no
        # coverage verdict to read. `failed` must never read as "nothing
        # wrong" (contract §2), so this is an absence, not a clean result.
        return None

    coverage = v2_integration.get("coverage")
    if not isinstance(coverage, list):
        return None

    grounded = 0
    decisive = 0
    unverified: list[str] = []
    for entry in coverage:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "")
        if status in GROUNDED_STATUSES:
            grounded += 1
        if status in DECISIVE_STATUSES:
            decisive += 1
        elif status in UNVERIFIED_STATUSES:
            part = str(entry.get("part") or "").strip()
            if part:
                unverified.append(part)

    citations = tuple(
        str(c) for c in (v2_integration.get("citations") or []) if str(c).strip()
    )
    raw_unsupported = v2_integration.get("unsupported_claims")
    unsupported = raw_unsupported if isinstance(raw_unsupported, int) else 0

    critique = v2_integration.get("critique")
    critic_ran = _critic_ran(ran, critique)

    return Grounding(
        checked=True,
        grounded_parts=grounded,
        unverified_parts=tuple(unverified),
        citations=citations,
        unsupported_claims=unsupported,
        decisive_parts=decisive,
        critic_ran=critic_ran,
    )


#: `ran` values that mean a section did NOT run. Everything else means it did.
#:
#: MATCHED BY MEANING, NOT BY EQUALITY WITH "ok". This gate used to read
#: `ran.get("critique") == "ok"`, and when the critic became deterministic the
#: value became "deterministic (verify_claims)" -- so the gate silently
#: returned False on every turn, forever. It failed in the safe direction
#: (`thin` simply never fires), which is worse than it sounds: a gate that can
#: never fire is dead code that still LOOKS live, and nothing would have
#: caught it because nothing asserts on a negative.
#:
#: A producer is free to change how it words "ok". It is not free to invent a
#: new way of saying "did not run" -- so the closed set goes on the failure
#: side, where a new value defaults to "it ran" rather than to silence.
_RAN_NOT_OK: frozenset[str] = frozenset({"", "skipped", "failed", "none", "error"})


def _ran_ok(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() not in _RAN_NOT_OK


def _critic_ran(ran: Any, critique: Any) -> bool:
    """Did a critic actually check anything?

    Two eras, and they disagree about what an empty critique means:

    - LLM critic, `ran == "ok"` exactly: empty meant the prompt blocks were
      missing from the prompt DB and it fell back. An ABSENT check, not a
      clean one (contract §2), so empty still reads as not-run.
    - Deterministic critic (verify_claims), any other wording: empty means it
      compared every claim against its cited page and flagged nothing. That
      is a real verdict and the thing this gate was always trying to find.

    Distinguished by the `ran` wording itself rather than by guessing, because
    the producer already says which one it is.
    """
    if not isinstance(ran, dict):
        return False
    value = ran.get("critique")
    if not _ran_ok(value):
        return False
    if str(value).strip().lower() == "ok":
        return bool(isinstance(critique, list) and critique)
    return True


def pages_per_part(v2_integration: dict[str, Any] | None) -> dict[str, int]:
    """DISTINCT pages backing each part, from coverage[].evidence.

    This is the `pages_per_entity` the answer-shape block wanted, and it was
    already in the contract -- coverage[] carries per-part evidence locators,
    so counting them needs nothing new from the producer. Asking for a field
    that already exists under another name is its own kind of second author.

    DISTINCT is the operative word: a live answer cited one payer twice and
    both were p5, which reads as two sources until the numbers are compared.
    """
    coverage = (v2_integration or {}).get("coverage")
    if not isinstance(coverage, list):
        return {}
    out: dict[str, int] = {}
    for entry in coverage:
        if not isinstance(entry, dict):
            continue
        part = str(entry.get("part") or "").strip()
        if not part:
            continue
        pages: set[str] = set()
        for locator in entry.get("evidence") or []:
            found = _PAGE_RE.findall(str(locator))
            # A locator with no page number is still one source.
            pages.update(found or [str(locator)])
        out[part] = len(pages)
    return out


_PAGE_RE = re.compile(r"\bp\.?\s*(\d+)\b", re.IGNORECASE)


def budget_from_v2(
    v2_integration: dict[str, Any] | None,
    is_raw_excerpt: bool = False,
    fallback_thin: bool = False,
) -> RenderBudget:
    """Build the classifier's RenderBudget from v2's verdict.

    `fallback_thin` is only consulted when v2 did not run. It is the caller's
    own heuristic, kept for turns v2 never touched rather than deleted -- but
    it no longer competes with the integrator on turns where the integrator
    spoke.
    """
    grounding = grounding_from_v2(v2_integration)
    if grounding is None:
        return RenderBudget(is_raw_excerpt=is_raw_excerpt, is_thin_evidence=fallback_thin)
    return RenderBudget(is_raw_excerpt=is_raw_excerpt, is_thin_evidence=grounding.thin)


def coverage_payload(v2_integration: dict[str, Any] | None) -> ContentPayload | None:
    """coverage[] as a typed payload -- part, what we concluded, what backs it.

    This is the three-payer check made visible: 17 passages covering two payers
    and 17 covering three read identically as prose and differently here. The
    status column is the point, so the table keeps every part including the
    ones nothing grounds.

    The COLOUR of each status is the frontend's (contract §2: grey for
    unobservable, never green, never red). This supplies the rows; it does not
    decide the treatment.
    """
    grounding = grounding_from_v2(v2_integration)
    if grounding is None:
        return None
    coverage = (v2_integration or {}).get("coverage")
    if not isinstance(coverage, list) or not coverage:
        return None

    rows: list[tuple[str, ...]] = []
    for entry in coverage:
        if not isinstance(entry, dict):
            continue
        part = str(entry.get("part") or "").strip()
        if not part:
            continue
        evidence = [str(e) for e in (entry.get("evidence") or []) if str(e).strip()]
        rows.append((
            part,
            str(entry.get("status") or "unobservable"),
            "; ".join(evidence) if evidence else "—",
        ))
    if not rows:
        return None
    return ContentPayload(
        table=TableData(headers=("Part", "Status", "Evidence"), rows=tuple(rows)),
        contiguous=True,
    )


def facts_payload(facts: Any) -> ContentPayload | None:
    """v2 facts[] as a typed payload.

    A Fact is {fact, document, page} -- a statement with provenance, not a
    shape. So this produces peer items with the source as a note, which is the
    honest reading of what a fact is. It is NOT an alternative to prose
    segmentation and does not replace it: both produce a ContentPayload, which
    is the whole point of the payload being the seam. If facts later carry a
    shape hint, this is where it is read, and nothing downstream changes.
    """
    if not isinstance(facts, (list, tuple)) or not facts:
        return None
    items: list[Item] = []
    for f in facts:
        if isinstance(f, dict):
            text = str(f.get("fact") or "").strip()
            doc = str(f.get("document") or "").strip()
            page = f.get("page")
        else:
            text, doc, page = str(f).strip(), "", None
        if not text:
            continue
        note = doc if not page else f"{doc} p{page}"
        items.append(Item(label=text, note=note))
    if not items:
        return None
    return ContentPayload(items=tuple(items), explicit_list=True, contiguous=True)


# --------------------------------------------------------------------------
# Facts as a section -- the ONE case where evidence may become a card
# --------------------------------------------------------------------------

#: Abstains that are a DELIBERATE REFUSAL, not an absence of structure.
#:
#: This is the distinction the whole thing turns on. "The formatter produced
#: no sections" is four different states, and two of them mean the formatter
#: declined ON PURPOSE:
#:
#:   abstain.no_match     there was no structure in the draft   -> facts ADD
#:   abstain.prose_list   the answer arrived as paragraphs      -> facts ADD
#:   abstain.raw_excerpt  the draft is a verbatim chunk         -> REFUSAL
#:   abstain.thin_evidence nothing grounds the answer           -> REFUSAL
#:
#: Rendering cited facts on a refusal UNDOES it -- and on thin_evidence it
#: does so in the worst possible direction, because a bullet list carrying
#: document and page next to an answer nothing grounds is the most
#: confident-looking thing on the screen. Triggering on "zero sections" alone
#: cannot tell these apart.
REFUSAL_RULE_IDS: frozenset[str] = frozenset(
    {"abstain.raw_excerpt", "abstain.thin_evidence"}
)

#: One bullet is not a list. Below two, the prose says it better and a section
#: would be structure for its own sake.
_MIN_FACT_BULLETS = 2


def may_render_facts(card: dict[str, Any]) -> bool:
    """Is this a card that facts are allowed to fill?

    Gated by construction rather than by the caller remembering: only a card
    with no sections AND no refusal on it qualifies. A card with an unknown
    or missing presentation block is treated as a non-refusal, because that
    is what it was before the abstain reasons existed.
    """
    if card.get("sections"):
        return False
    rule_id = (card.get("presentation") or {}).get("rule_id")
    return rule_id not in REFUSAL_RULE_IDS


def fact_bullets(facts: Any) -> list[str]:
    """Grounded facts as cited bullet text, deduplicated.

    ONLY grounded facts. A fact with no document is exactly the ungrounded
    claim the v2 contract exists to keep out of an answer, and promoting one
    into a section would give it MORE prominence than the prose did.

    Deduplicated on normalised text, because the same fact restated across
    rounds is one thing learned, not two.
    """
    seen: set[str] = set()
    out: list[str] = []
    for f in facts or ():
        if not getattr(f, "grounded", False):
            continue
        text = (getattr(f, "fact", "") or "").strip()
        key = " ".join(text.lower().split())
        if not text or key in seen:
            continue
        seen.add(key)
        doc = (getattr(f, "document", "") or "").strip()
        page = getattr(f, "page", None)
        cite = (f" [{doc} p{page}]" if page else f" [{doc}]") if doc else ""
        out.append(f"{text}{cite}")
    return out


def add_fact_sections(card: dict[str, Any], facts: Any) -> dict[str, Any]:
    """Fill an empty card from v2's grounded facts, when that is honest.

    WHY THIS IS NOT THE THING classify_envelope REFUSES TO DO. The classifier
    declines to infer structure out of prose, because guessing at structure is
    how a renderer invents emphasis the author never meant. A fact is not
    prose: it arrived as a typed statement carrying its own document and page.
    Rendering it is presenting structure we were GIVEN.

    The card is mutated and returned, matching the caller's existing shape.
    """
    if not may_render_facts(card):
        return card
    bullets = fact_bullets(facts)
    if len(bullets) < _MIN_FACT_BULLETS:
        return card
    card.setdefault("sections", []).append({
        "intent": "references",
        "label": "What the documents say",
        "format": "bullets",
        "bullets": bullets,
        "visibility": "primary",
    })
    # The card is no longer an abstain, so the note explaining why it had no
    # sections would now contradict what is on screen.
    card.pop("presentation", None)
    return card


def is_v2_turn(ctx: Any) -> bool:
    """Did the v2 loop serve this turn?

    Read from what the turn HOLDS, never from the env: a percentage rollout
    means the flag is on while individual turns are still assigned to v1, so
    an env check would treat control-arm turns as v2. ctx.v2_integration is
    set by react_loop when v2's integrator runs, and _v2_last_contract when
    react answered under the v2 contract. Neither is present on a v1 turn.

    Lives here rather than in integrate.py because react_loop needs the same
    test, and two copies of this expression would drift the first time the
    posture changed -- which is the argument Governor made for _communicating
    and it applies identically.
    """
    return bool(getattr(ctx, "v2_integration", None)
                or getattr(ctx, "_v2_last_contract", None))
