"""The statement registry: what the governor tells react, and when.

Ananth, 2026-09-12: *"each of them is dynamic... you have to tell which of this
and what posture against each."*

THE DESIGN IS THE SELECTOR, NOT THE TEXT. A statement with no selector is a
constant nobody chose. A selector reading a field that does not exist is a
statement that never fires -- which is how `gap_targeted` was computed for 417
decisions and read by nothing.

THREE ORDERINGS, and conflating them is the bug (docs/governor-profile-
statements.md):

  TURN       which rounds a statement may fire in
  EXECUTION  the order react performs work IN a round -- this is the RENDER
             order, because the model reads top-to-bottom and acts in sequence
  PRECEDENCE which statement wins when two could fire -- INCLUSION ONLY

E1 (BOUND) renders first because "this is the final round" invalidates every
statement below it; render it last and the model has planned a tool call before
it learns it cannot make one. E4 (SETTLE?) renders before E5 (TARGET) because
that IS the dual mode -- ask whether we are done before deciding what to spend
on. Reversed, the governor picks a gap and the completion question is never put.

PURE. State in, statements out. No clock, no DB, no env -- same bar as
posture.py, and for the same reason: a prompt that cannot be rebuilt from a
stored row cannot be replayed when an answer is disputed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Callable

from app.pipeline.v2.posture import (
    ROOT_GAP_ID, Gap, Posture, RoundState, Trend, closure_trend,
    latest_closure, may_overrun, spendable, stuck, targeted_attempts, trend,
    unreachable,
)

# The cap. More than this is a wall of text the model skims, and the statement
# that mattered is the one it skipped. A DROPPED statement is recorded --
# silently dropping guidance is how a rule stops being sent without anyone
# noticing.
MAX_STATEMENTS = 5


class Slot(IntEnum):
    """EXECUTION order == RENDER order. Lower renders first."""
    BOUND = 1        # what constrains this round
    ORIENT = 2       # what is being asked
    REVIEW = 3       # what I have, what is missing
    SETTLE = 4       # is this complete  -- BEFORE spending
    TARGET = 5       # which gap this round
    APPROACH = 6     # new / retry / change lever / escalate
    ACT = 7          # choose tool, write query
    RECORD = 9       # closure, gaps
    SYNTHESISE = 10  # running answer, grounded, partial honestly
    FORM = 11        # shape, length, confidence, next steps


class Group(IntEnum):
    """PRECEDENCE. Inclusion only -- never render order. At most ONE statement
    per group fires. A statement can win precedence and still render sixth."""
    SAFETY = 1
    CONTROL = 2
    STRATEGY = 3
    EVIDENCE = 4
    FRAMING = 5
    FORM = 6


ANY_POSTURE: frozenset[Posture] = frozenset(Posture)


@dataclass(frozen=True)
class Ctx:
    """Everything a selector may read. Nothing else is in scope -- a selector
    that needs a new field must add it here, which makes the dependency
    visible instead of reaching into ctx and finding whatever is there."""
    state: RoundState
    round_index: int
    max_rounds: int | None = None
    tier: str = "normal"                 # fast | normal | thinking
    model_proposes_complete: bool = False
    kept: int = 0
    gap_status: str = ""                 # progressing | stagnant
    extensions_used: int = 0
    extensions_max: int = 6
    gap: Gap | None = None               # worth_spending(), the machine's pick

    @property
    def material(self) -> tuple[Gap, ...]:
        return self.state.open_gaps


@dataclass(frozen=True)
class Statement:
    id: str
    slot: Slot
    group: Group
    postures: frozenset[Posture]
    when: Callable[[Ctx], bool]
    # WORDING LIVES IN THE PROMPT DB, keyed by this. See statement_text.py.
    # Selection is code -- tested, typed, and it must not change without a
    # review. Wording is content -- it needs iterating without a deploy and a
    # prompt seat owns it. One lambda held both an hour ago, which is how
    # react_loop grew 6,900 lines of prose nobody can edit without shipping
    # Python.
    block_key: str
    # What the wording interpolates, drawn from the ledger. The governor
    # supplies FACTS; the block decides how to say them. A param the block does
    # not use costs nothing; a fact the governor does not supply cannot be
    # recovered by rewording.
    params: Callable[[Ctx], dict] = lambda c: {}
    # Statements that may never be rendered alongside this one. react_loop
    # already uses `elif` for exactly this hazard: "you're not required to
    # stop" directly contradicts "do NOT request another tool call".
    conflicts: frozenset[str] = frozenset()


def _q(gap: Gap | None) -> str:
    return (gap.text if gap else "the open part")


def _last_query(gap: Gap | None) -> str:
    if not gap:
        return "(not recorded)"
    qs = [a.query for a in targeted_attempts(gap) if a.query]
    return ", ".join(f'"{q}"' for q in qs) or "(not recorded)"


def _named_gap(c: Ctx) -> Gap | None:
    """The gap a statement may SPEAK ABOUT, or None.

    THE ROOT GAP IS THE QUESTION, NOT A PART. Rendering it into a gap-shaped
    sentence produces lines like "one more round is authorised if it closes
    <the entire question>" and "<the entire question> was searched once and is
    still open" -- both true of a ledger row and both nonsense to read.

    Found by rendering round 2 locally rather than deploying: EVD-4 already
    excluded the root, and five other statements did not, because the
    exclusion was written as one statement's condition instead of as the
    property it actually is.
    """
    g = c.gap
    return None if (g is None or g.gap_id == ROOT_GAP_ID) else g


def _unattempted(c: Ctx) -> Gap | None:
    """A material gap with no attempt KNOWN to have aimed at it.

    This is the fact react cannot reliably hold and the governor can -- and it
    only became knowable when Attempt.targeted shipped. Before that, one Molina
    query marked all three payer gaps attempted.
    """
    for g in c.material:
        if not targeted_attempts(g):
            return g
    return None


REGISTRY: tuple[Statement, ...] = (

    # ── E1 BOUND ────────────────────────────────────────────────────────────
    Statement("SAF-1", Slot.BOUND, Group.SAFETY, ANY_POSTURE,
              when=lambda c: c.max_rounds is not None and c.round_index >= c.max_rounds,
              block_key="governor.saf_final_round",
              conflicts=frozenset({"CTL-5", "STR-1", "STR-2", "STR-3", "EVD-4", "EVD-2"})),
    Statement("SAF-3", Slot.BOUND, Group.SAFETY, ANY_POSTURE,
              when=lambda c: c.extensions_used >= c.extensions_max,
              block_key="governor.saf_ceiling",
              conflicts=frozenset({"CTL-5", "EVD-4"})),
    Statement("CTL-4", Slot.BOUND, Group.CONTROL,
              frozenset({Posture.NARROW, Posture.ALTERNATIVES, Posture.COMMUNICATE}),
              when=lambda c: not spendable(c.state) and not may_overrun(c.state)[0],
              block_key="governor.ctl_budget_spent",
              conflicts=frozenset({"CTL-5", "EVD-4", "STR-1", "STR-2", "STR-3"})),
    Statement("CTL-5", Slot.BOUND, Group.CONTROL,
              frozenset({Posture.EXPLORE, Posture.VALIDATE}),
              when=lambda c: may_overrun(c.state)[0] and _named_gap(c) is not None,
              block_key="governor.ctl_overrun_authorised",
              params=lambda c: {"gap": _q(c.gap)}),

    # ── E2 ORIENT ───────────────────────────────────────────────────────────
    Statement("FRM-1", Slot.ORIENT, Group.FRAMING, ANY_POSTURE,
              when=lambda c: c.round_index == 1,
              block_key="governor.frm_parts_are_a_report"),
    Statement("FRM-2", Slot.ORIENT, Group.FRAMING, ANY_POSTURE,
              when=lambda c: c.round_index == 1,
              block_key="governor.frm_ask_as_asked"),
    Statement("FRM-4", Slot.ORIENT, Group.FRAMING, ANY_POSTURE,
              when=lambda c: c.round_index == 1 and c.tier == "thinking",
              block_key="governor.frm_verify"),

    # ── E3 REVIEW ───────────────────────────────────────────────────────────
    Statement("EVD-1", Slot.REVIEW, Group.EVIDENCE,
              frozenset({Posture.EXPLORE, Posture.FRAME}),
              when=lambda c: c.round_index == 2 and c.gap is not None
                             and c.gap.gap_id == ROOT_GAP_ID,
              block_key="governor.evd_review_not_reask"),
    Statement("EVD-3", Slot.REVIEW, Group.EVIDENCE, ANY_POSTURE,
              when=lambda c: c.round_index > 1 and c.kept == 0,
              block_key="governor.evd_nothing_kept"),

    # ── E4 SETTLE? — the handshake, before spending ─────────────────────────
    Statement("CTL-3", Slot.SETTLE, Group.CONTROL, ANY_POSTURE,
              when=lambda c: True,
              block_key="governor.ctl_satisfied"),
    Statement("CTL-1", Slot.SETTLE, Group.CONTROL, ANY_POSTURE,
              when=lambda c: c.model_proposes_complete and _unattempted(c) is not None,
              block_key="governor.ctl_dissent",
              params=lambda c: {"gap": _q(_unattempted(c))}),

    # ── E5 TARGET ───────────────────────────────────────────────────────────
    Statement("EVD-4", Slot.TARGET, Group.EVIDENCE,
              frozenset({Posture.EXPLORE, Posture.VALIDATE}),
              when=lambda c: _named_gap(c) is not None and len(c.material) > 1,
              block_key="governor.evd_work_this_gap",
              params=lambda c: {"gap": _q(c.gap)}),

    # ── E6 APPROACH ─────────────────────────────────────────────────────────
    Statement("STR-1", Slot.APPROACH, Group.STRATEGY, frozenset({Posture.EXPLORE}),
              when=lambda c: _named_gap(c) is not None and len(targeted_attempts(c.gap)) == 1,
              block_key="governor.str_searched_once",
              params=lambda c: {"gap": _q(c.gap), "prior_queries": _last_query(c.gap)},
              # Both say "stay on this gap and ask differently". Two near-
              # identical lines teach the model to skim the block.
              conflicts=frozenset({"STR-3"})),
    Statement("STR-2", Slot.APPROACH, Group.STRATEGY, frozenset({Posture.EXPLORE}),
              when=lambda c: _named_gap(c) is not None and stuck(c.gap, c.round_index),
              block_key="governor.str_stuck",
              params=lambda c: {"gap": _q(c.gap), "prior_queries": _last_query(c.gap)}),
    Statement("STR-3", Slot.APPROACH, Group.STRATEGY, frozenset({Posture.EXPLORE}),
              when=lambda c: _named_gap(c) is not None
                             and closure_trend(c.gap) is Trend.INCREASING,
              block_key="governor.str_closing",
              params=lambda c: {"gap": _q(c.gap)}),
    Statement("STR-4", Slot.APPROACH, Group.STRATEGY, frozenset({Posture.EXPLORE}),
              when=lambda c: _named_gap(c) is not None
                             and closure_trend(c.gap) is Trend.DECREASING,
              block_key="governor.str_falling",
              params=lambda c: {"gap": _q(c.gap)}),
    Statement("STR-5", Slot.APPROACH, Group.STRATEGY,
              frozenset({Posture.ALTERNATIVES, Posture.EXPLORE}),
              when=lambda c: _named_gap(c) is not None and c.tier == "thinking"
                             and unreachable(c.gap, c.round_index),
              block_key="governor.str_unreachable",
              params=lambda c: {"gap": _q(c.gap)}),
    Statement("STR-6", Slot.APPROACH, Group.STRATEGY,
              frozenset({Posture.NARROW, Posture.EXPLORE}),
              when=lambda c: c.round_index >= 3
                             and trend(c.state.gaps_open_history) is Trend.INCREASING,
              block_key="governor.str_widening"),
    Statement("STR-7", Slot.APPROACH, Group.STRATEGY, frozenset({Posture.EXPLORE}),
              when=lambda c: c.gap_status == "stagnant",
              block_key="governor.str_stagnant"),

    # ── E7 ACT ──────────────────────────────────────────────────────────────
    Statement("EVD-2", Slot.ACT, Group.EVIDENCE, ANY_POSTURE,
              when=lambda c: c.round_index > 1,
              block_key="governor.evd_do_not_repeat",
              conflicts=frozenset({"SAF-1"})),

    # ── E10 SYNTHESISE ──────────────────────────────────────────────────────
    Statement("FRM-7", Slot.SYNTHESISE, Group.FORM, ANY_POSTURE,
              when=lambda c: c.round_index > 1 and (
                             any(not targeted_attempts(g) for g in c.material)
                             or any(targeted_attempts(g)
                                    and not any(a.returned_payload
                                                for a in targeted_attempts(g))
                                    for g in c.material)),
              block_key="governor.frm_say_why_missing"),
)


def text_of(s: Statement, c: Ctx) -> tuple[str, str]:
    """(text, source). Resolution is IMPURE -- it reads the prompt DB -- so it
    lives outside select() and this module stays pure for selection."""
    from app.pipeline.v2.statement_text import resolve
    try:
        params = s.params(c) or {}
    except Exception:
        params = {}
    return resolve(s.block_key, params)


@dataclass(frozen=True)
class Selection:
    """What was chosen, and what was dropped. Both are the record."""
    statements: tuple[Statement, ...]
    dropped_by_cap: tuple[str, ...] = ()
    dropped_by_conflict: tuple[str, ...] = ()

    def render(self) -> str | None:
        if not self.statements:
            return None
        lines = ["[Governor]"]
        raise NotImplementedError("use statements.render(); text needs a Ctx")
        return "\n".join(lines)


def select(c: Ctx, posture: Posture) -> Selection:
    """Which statements this round gets, in RENDER order.

    Order of operations, and each step is load-bearing:
      1. posture + selector        -- eligibility
      2. conflicts                 -- an explicitly contradictory pair is
                                      resolved by GROUP (lower wins), and the
                                      loser is RECORDED
      3. cap                       -- highest Slot dropped first: the latest-
                                      executing guidance is the most skippable
      4. sort by Slot              -- execution order == render order

    🔴 THERE IS NO "ONE PER GROUP" RULE, and there was for about an hour.

    It made the governor's dissent (CTL-1) STRUCTURALLY UNREACHABLE: CTL-3 is
    also CONTROL and sorted first, so the one statement that exists to ask
    "you said complete, but this part was never searched" could never be sent.
    That is the same defect as every other failure this session -- a precedence
    rule that removes a capability rather than ordering it -- and it was
    invisible until the block was RENDERED for a round where both applied.

    It also silently dropped FRM-2 ("ask the question as asked") because FRM-1
    ("name every part") is also FRAMING. Those two are COMPLEMENTARY, not
    rivals: together they are the whole round-1 instruction, and keeping only
    the first is how a question gets decomposed and then asked one part at a
    time -- the exact behaviour this was built to stop.

    Contradiction is now handled ONLY by explicit `conflicts`, which names the
    pair and can be tested. Volume is handled by the cap. Group survives as a
    tie-break for conflicts, nothing more.
    """
    eligible = [s for s in REGISTRY
                if posture in s.postures and _safe(s.when, c)]
    kept = sorted(eligible, key=lambda s: (s.slot, s.group))

    survivors: list[Statement] = []
    conflicted: list[str] = []
    for s in kept:
        if any(s.id in k.conflicts or k.id in s.conflicts for k in survivors):
            conflicted.append(s.id)
            continue
        survivors.append(s)

    capped: list[str] = []
    while len(survivors) > MAX_STATEMENTS:
        capped.append(survivors.pop(-1).id)

    return Selection(tuple(survivors), tuple(capped), tuple(conflicted))


def _safe(fn: Callable[[Ctx], bool], c: Ctx) -> bool:
    """A selector that raises must not take the turn with it. It returns
    False -- absent guidance, never a crashed round -- and that is a
    deliberate fail-quiet on the PROMPT path only."""
    try:
        return bool(fn(c))
    except Exception:
        return False


def render(c: Ctx, posture: Posture) -> tuple[str | None, Selection]:
    """The block, and the record of how it was built."""
    sel = select(c, posture)
    if not sel.statements:
        return None, sel
    lines = ["[Governor]"] + [f"- {text_of(s, c)[0]}" for s in sel.statements]
    return "\n".join(lines), sel
