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
    text: Callable[[Ctx], str]
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
    Statement(
        "SAF-1", Slot.BOUND, Group.SAFETY, ANY_POSTURE,
        when=lambda c: c.max_rounds is not None and c.round_index >= c.max_rounds,
        text=lambda c: "This is the final round. Answer from what you have; "
                       "do not request another tool call.",
        conflicts=frozenset({"CTL-5", "STR-1", "STR-2", "STR-3", "EVD-4"}),
    ),
    Statement(
        "SAF-3", Slot.BOUND, Group.SAFETY, ANY_POSTURE,
        when=lambda c: c.extensions_used >= c.extensions_max,
        text=lambda c: "The extension ceiling is reached. This is the last "
                       "round regardless of what is still open.",
        conflicts=frozenset({"CTL-5", "EVD-4"}),
    ),
    Statement(
        "CTL-4", Slot.BOUND, Group.CONTROL,
        frozenset({Posture.NARROW, Posture.ALTERNATIVES, Posture.COMMUNICATE}),
        when=lambda c: not spendable(c.state) and not may_overrun(c.state)[0],
        text=lambda c: "The time budget is spent. Answer from what you have "
                       "and say plainly what is still open.",
        conflicts=frozenset({"CTL-5", "EVD-4", "STR-1", "STR-2", "STR-3"}),
    ),
    Statement(
        "CTL-5", Slot.BOUND, Group.CONTROL, frozenset({Posture.EXPLORE, Posture.VALIDATE}),
        when=lambda c: may_overrun(c.state)[0] and c.gap is not None,
        text=lambda c: f"You are past the time target but converging. One more "
                       f"round is authorised if it closes {_q(c.gap)!r} -- not "
                       f"for polish.",
    ),

    # ── E2 ORIENT ───────────────────────────────────────────────────────────
    Statement(
        "FRM-1", Slot.ORIENT, Group.FRAMING, ANY_POSTURE,
        when=lambda c: c.round_index == 1,
        text=lambda c: "Name every part this question asks for in gaps_open. "
                       "That list is a REPORT of what was asked -- not a plan "
                       "to do them one at a time.",
    ),
    Statement(
        "FRM-2", Slot.ORIENT, Group.FRAMING, ANY_POSTURE,
        when=lambda c: c.round_index == 1,
        text=lambda c: "Ask the question as asked: one query naming every "
                       "part. rag decomposes across named entities better "
                       "than asking about them one at a time.",
    ),
    Statement(
        "FRM-4", Slot.ORIENT, Group.FRAMING, ANY_POSTURE,
        when=lambda c: c.round_index == 1 and c.tier == "thinking",
        text=lambda c: "You have rounds to spend. Verify rather than accept "
                       "the first plausible match.",
    ),

    # ── E3 REVIEW ───────────────────────────────────────────────────────────
    Statement(
        "EVD-1", Slot.REVIEW, Group.EVIDENCE, frozenset({Posture.EXPLORE, Posture.FRAME}),
        when=lambda c: c.round_index == 2 and c.gap is not None
                       and c.gap.gap_id == ROOT_GAP_ID,
        text=lambda c: "Review, do not re-ask. Compare what you now have "
                       "against what was asked, and name EACH part still "
                       "missing or thin as its own gap in gaps_open. A part "
                       "you have already covered is not a gap.",
    ),
    Statement(
        "EVD-3", Slot.REVIEW, Group.EVIDENCE, ANY_POSTURE,
        when=lambda c: c.round_index > 1 and c.kept == 0,
        text=lambda c: "Nothing was kept from the last call. Either the query "
                       "missed, or this corpus does not hold it -- say which "
                       "you think it is.",
    ),

    # ── E4 SETTLE? — the handshake, before spending ─────────────────────────
    Statement(
        "CTL-3", Slot.SETTLE, Group.CONTROL, ANY_POSTURE,
        when=lambda c: True,     # CONSTANT: the effort question, always asked
        text=lambda c: "Before is_complete=true: are you satisfied with the "
                       "level of answer and evidence you have? Not 'is the "
                       "answer grounded' -- 'did I do enough to get it'. A "
                       "part left unanswered because you never looked is not "
                       "complete.",
    ),
    Statement(
        "CTL-1", Slot.SETTLE, Group.CONTROL, ANY_POSTURE,
        when=lambda c: c.model_proposes_complete and _unattempted(c) is not None,
        text=lambda c: (
            f"You marked this complete. My ledger shows "
            f"{_q(_unattempted(c))!r} was never searched this turn -- no query "
            f"named it. Confirm complete, or spend one round on it. Your call."
        ),
    ),

    # ── E5 TARGET ───────────────────────────────────────────────────────────
    Statement(
        "EVD-4", Slot.TARGET, Group.EVIDENCE, frozenset({Posture.EXPLORE, Posture.VALIDATE}),
        when=lambda c: c.gap is not None and len(c.material) > 1
                       and c.gap.gap_id != ROOT_GAP_ID,
        text=lambda c: f"Work {_q(c.gap)!r} this round. The other open parts "
                       f"stay open and are not for this round.",
    ),

    # ── E6 APPROACH ─────────────────────────────────────────────────────────
    Statement(
        "STR-1", Slot.APPROACH, Group.STRATEGY, frozenset({Posture.EXPLORE}),
        when=lambda c: c.gap is not None and len(targeted_attempts(c.gap)) == 1,
        text=lambda c: f"{_q(c.gap)!r} was searched once and is still open. "
                       f"Previous query: {_last_query(c.gap)}. Ask for the "
                       f"part it did not return.",
    ),
    Statement(
        "STR-2", Slot.APPROACH, Group.STRATEGY, frozenset({Posture.EXPLORE}),
        when=lambda c: c.gap is not None and stuck(c.gap, c.round_index),
        text=lambda c: f"{_q(c.gap)!r}: several distinct levers, nothing "
                       f"returned. Previous queries: {_last_query(c.gap)}. A "
                       f"reworded query returns the same evidence -- change "
                       f"the approach or say it cannot be closed.",
    ),
    Statement(
        "STR-3", Slot.APPROACH, Group.STRATEGY, frozenset({Posture.EXPLORE}),
        when=lambda c: c.gap is not None
                       and closure_trend(c.gap) is Trend.INCREASING,
        text=lambda c: f"{_q(c.gap)!r} is closing. Stay on it and ask for the "
                       f"part still missing.",
    ),
    Statement(
        "STR-4", Slot.APPROACH, Group.STRATEGY, frozenset({Posture.EXPLORE}),
        when=lambda c: c.gap is not None
                       and closure_trend(c.gap) is Trend.DECREASING,
        text=lambda c: f"Closure on {_q(c.gap)!r} fell -- the last attempt "
                       f"moved away from it. Return to what was working.",
    ),
    Statement(
        "STR-5", Slot.APPROACH, Group.STRATEGY,
        frozenset({Posture.ALTERNATIVES, Posture.EXPLORE}),
        when=lambda c: c.gap is not None and c.tier == "thinking"
                       and unreachable(c.gap, c.round_index),
        text=lambda c: f"{_q(c.gap)!r} looks unreachable in this corpus. "
                       f"Before declaring it, try one materially different "
                       f"source class.",
    ),
    Statement(
        "STR-6", Slot.APPROACH, Group.STRATEGY, frozenset({Posture.NARROW, Posture.EXPLORE}),
        when=lambda c: c.round_index >= 3
                       and trend(c.state.gaps_open_history) is Trend.INCREASING,
        text=lambda c: "The open parts are growing, not shrinking. Stop "
                       "widening and close one.",
    ),
    Statement(
        "STR-7", Slot.APPROACH, Group.STRATEGY, frozenset({Posture.EXPLORE}),
        when=lambda c: c.gap_status == "stagnant",
        text=lambda c: "The last two rag calls converged on the same internal "
                       "strategy and outcome. Another rag call with a similar "
                       "query will not surface new information.",
    ),

    # ── E7 ACT ──────────────────────────────────────────────────────────────
    Statement(
        "EVD-2", Slot.ACT, Group.EVIDENCE, ANY_POSTURE,
        when=lambda c: c.round_index > 1,
        text=lambda c: "Do not repeat the query you just ran. It returned what "
                       "it returned; this round is for what it did not.",
        conflicts=frozenset({"SAF-1"}),
    ),

    # ── E10 SYNTHESISE ──────────────────────────────────────────────────────
    Statement(
        "FRM-7", Slot.SYNTHESISE, Group.FORM, ANY_POSTURE,
        # NOT on round 1: nothing has been tried yet, so "say why you could
        # not answer this part" is a question about work that has not happened.
        when=lambda c: c.round_index > 1 and (
                       any(not targeted_attempts(g) for g in c.material)
                       or any(targeted_attempts(g)
                              and not any(a.returned_payload
                                          for a in targeted_attempts(g))
                              for g in c.material)),
        text=lambda c: (
            "For each part you could not answer, say WHICH of these it is: "
            "(a) searched, the corpus is thin; (b) searched, nothing relevant "
            "came back; (c) not searched -- the turn ran out of budget. Do "
            "NOT render all three as \"not available in the provided "
            "documents\": they are different facts and carry opposite advice."
        ),
    ),
)


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
        lines += [f"- {s.text(self._ctx)}" for s in self.statements]
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
    lines = ["[Governor]"] + [f"- {s.text(c)}" for s in sel.statements]
    return "\n".join(lines), sel
