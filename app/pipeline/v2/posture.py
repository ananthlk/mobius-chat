"""The posture machine: what the next round is for, and whether to buy it.

PURE. No clock, no DB, no globals. Every input is an argument.

Spec: docs/governor-system-logic.md Part II
      docs/governor-gap-ledger-and-posture.md
      docs/orchestrator-v2-port-hazards.md   <- read before changing the loop

CONSTANTS TAGGED [GUESS] ARE CALIBRATED ON NOTHING. The measurements that
motivated them (docs/governor-gap-ledger-and-posture.md section 2) count UNNAMED
strings -- two "gaps" may be one gap rephrased. They argue the ledger is worth
building; they are NOT the calibration. Re-derive on real gap ids before any of
these decides anything in production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# ── constants ────────────────────────────────────────────────────────────────

STUCK_AGE_ROUNDS = 3          # [GUESS] fires on 182/837 multi-round turns
STUCK_MIN_LEVERS = 2          # [GUESS] distinct attempts before "change lever"
REPEAT_JACCARD = 0.70         # [GUESS] 24.5% of consecutive rag pairs exceed it
TREND_WINDOW = 2              # rounds compared to classify direction


class Posture(str, Enum):
    FRAME = "frame"
    EXPLORE = "explore"
    NARROW = "narrow"
    VALIDATE = "validate"
    COMMUNICATE = "communicate"


class Directive(str, Enum):
    """Directives on EXPLORE. Two, not three.

    REFORMULATE folded into CLOSE on the prompt seat's argument (2026-09-11):
    it is not a different aim, it is the same aim on a second attempt. The
    distinguishing fact is whether attempted_by already has an entry -- data the
    governor already holds -- not a name the governor asserts. The prompt
    receives the gap WITH its attempt history and the materiality requirement
    follows from the data.
    """
    DISCOVER = "discover"
    CLOSE = "close"


class ExitMode(str, Enum):
    COMPLETE = "complete"
    BUDGET = "budget"
    CAPABILITY = "capability"
    ERROR = "error"


class Trend(str, Enum):
    INCREASING = "increasing"
    FLAT = "flat"
    DECREASING = "decreasing"


@dataclass(frozen=True)
class Attempt:
    """One lever already spent on a gap.

    ``returned_payload`` is the PAYLOAD CHECK, never the tool's own success
    flag. Port hazard 9: ReactRetryGuard._is_zero_result cannot fire for MCP
    tools because the adapter attaches a self-citing SourceRef on success, so
    the escape hatch is always satisfied and exhaustion is structurally
    unreachable for ~34 tools. A tool saying "I succeeded" and a tool having
    produced something are different facts, and the exit mode depends on the
    second.
    """
    round_index: int
    tool: str | None = None
    model: str | None = None
    query: str | None = None
    returned_payload: bool = False


@dataclass(frozen=True)
class Gap:
    gap_id: str
    text: str
    opened_round: int
    importance: str = "normal"
    attempted_by: tuple[Attempt, ...] = ()

    def age(self, current_round: int) -> int:
        return max(0, current_round - self.opened_round)


@dataclass(frozen=True)
class Budget:
    """What is left. Cost is carried but MUST NOT decide -- see spendable()."""
    remaining_s: float
    remaining_c: float
    cost_coverage: float = 1.0   # 686/1236 on react_1 today


@dataclass(frozen=True)
class RoundState:
    """Everything the machine is allowed to look at."""
    round_index: int
    open_gaps: tuple[Gap, ...]
    gaps_open_history: tuple[int, ...]     # counts, oldest first
    budget: Budget
    next_round_cost_s: float
    acting_cost_s: float                   # cost of ACTING on what a round finds
    validate_cost_s: float
    errored: bool = False
    quality_uncertain: bool = False
    min_importance: str = "normal"


_IMPORTANCE_ORDER = {"low": 0, "normal": 1, "high": 2}


# ── signals ──────────────────────────────────────────────────────────────────

def jaccard(a: str, b: str) -> float:
    """Word-level overlap. Used to check COMPLIANCE, not to select a directive.

    Prevention and verification are different mechanisms and we need both:
    injecting prior attempts prevents the shallow retry; measuring overlap
    verifies the prevention worked. A failure here means the instruction was
    ignored -- a prompt finding, not a posture decision.
    """
    wa = {w for w in (a or "").lower().split() if w}
    wb = {w for w in (b or "").lower().split() if w}
    if not wa and not wb:
        return 1.0
    union = wa | wb
    return len(wa & wb) / len(union) if union else 0.0


def complied(gap: Gap, new_query: str) -> bool:
    """Did the new query differ materially from every prior attempt on this gap?"""
    return all(
        jaccard(new_query, a.query or "") < REPEAT_JACCARD
        for a in gap.attempted_by
        if a.query
    )


def trend(history: tuple[int, ...]) -> Trend:
    """Direction of the open-gap count.

    Only INCREASING carries information. Measured: decreasing 41.5%, flat 41.0%,
    increasing 25.4% -- decreasing and flat are indistinguishable. The LEVEL
    carries nothing at all (1 gap 24.6%, 4 gaps 23.9%) because the list GROWS as
    evidence arrives, so the denominator moves and "% closed" is not a
    percentage.
    """
    if len(history) < TREND_WINDOW:
        return Trend.FLAT
    now, prev = history[-1], history[-TREND_WINDOW]
    if now > prev:
        return Trend.INCREASING
    if now < prev:
        return Trend.DECREASING
    return Trend.FLAT


def distinct_levers(gap: Gap) -> int:
    return len({(a.tool, a.model) for a in gap.attempted_by})


def stuck(gap: Gap, current_round: int) -> bool:
    """The lever is not working. Change it -- do not buy another round of it."""
    return (
        gap.age(current_round) >= STUCK_AGE_ROUNDS
        and distinct_levers(gap) >= STUCK_MIN_LEVERS
        and not any(a.returned_payload for a in gap.attempted_by)
    )


def spendable(state: RoundState) -> bool:
    """Reserve the cost of ACTING, not just the cost of looking.

    Buying the last round of budget to DISCOVER a problem leaves nothing to fix
    it with. Cost is deliberately absent from this check: react_1 reports cost on
    686 of 1,236 calls, so spend is understated on precisely the path the
    governor steers, and an understated spend FAILS OPEN -- it permits rounds it
    should refuse. Cost may INFORM a decision; it must not DECIDE one. Time can
    decide; the promise clock is complete.
    """
    return state.budget.remaining_s >= (state.next_round_cost_s + state.acting_cost_s)


def worth_spending(state: RoundState) -> Gap | None:
    floor = _IMPORTANCE_ORDER.get(state.min_importance, 1)
    if not spendable(state):
        return None
    for gap in state.open_gaps:
        if _IMPORTANCE_ORDER.get(gap.importance, 1) < floor:
            continue
        if stuck(gap, state.round_index):
            continue
        return gap
    return None


def validate_worth_it(state: RoundState) -> bool:
    """Value of information: spend on knowing only when knowing can change doing.

    Never a mode test. `agentic` was a proxy for expected difficulty, which is a
    forecast -- replace it with the forecast, not another proxy. A verdict with
    nowhere to go is pure cost, so the fix round is reserved, not just the look.
    """
    affordable = state.budget.remaining_s >= (state.validate_cost_s + state.acting_cost_s)
    return affordable and state.quality_uncertain


# ── the machine ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Decision:
    posture: Posture
    because: str
    directive: Directive | None = None
    gap_targeted: str | None = None
    prior_queries: tuple[str, ...] = field(default_factory=tuple)


def select(state: RoundState) -> Decision:
    """One posture per round. Skipping is what the tier IS."""
    if state.errored:
        return Decision(Posture.COMMUNICATE, "errored: one exit for every path")

    if not state.open_gaps and state.round_index <= 1:
        return Decision(Posture.FRAME, "no gap list yet")

    if trend(state.gaps_open_history) is Trend.INCREASING:
        return Decision(
            Posture.EXPLORE,
            "gaps increasing: still discovering, closure is 25.4% here",
            directive=Directive.DISCOVER,
        )

    gap = worth_spending(state)
    if gap is not None:
        return Decision(
            Posture.EXPLORE,
            f"closing {gap.gap_id} (open {gap.age(state.round_index)} rounds, "
            f"{distinct_levers(gap)} levers spent)",
            directive=Directive.CLOSE,
            gap_targeted=gap.gap_id,
            prior_queries=tuple(a.query for a in gap.attempted_by if a.query),
        )

    if state.open_gaps:
        return Decision(
            Posture.NARROW,
            "gaps open but none worth buying: scope and name what is left",
        )

    if validate_worth_it(state):
        return Decision(Posture.VALIDATE, "quality uncertain and a fix round is affordable")

    return Decision(Posture.COMMUNICATE, "no gap worth spending on")


def exit_mode(state: RoundState) -> ExitMode:
    """Semantic outcome. NOT the same axis as the mechanical `outcome` field.

    A turn can be mechanically `completed` and semantically `budget`, and that is
    the common case. Collapsing them gives a report saying "we completed 98% of
    turns" while never saying "40% shipped with open gaps".
    """
    if state.errored:
        return ExitMode.ERROR

    floor = _IMPORTANCE_ORDER.get(state.min_importance, 1)
    material = [
        g for g in state.open_gaps
        if _IMPORTANCE_ORDER.get(g.importance, 1) >= floor
    ]
    if not material:
        return ExitMode.COMPLETE

    # CAPABILITY: every attempt on every material gap RAN and produced nothing.
    # A gap never attempted is BUDGET -- we ran out before trying it, which is a
    # different fact and carries the opposite advice about continuing.
    def exhausted(g: Gap) -> bool:
        return bool(g.attempted_by) and not any(a.returned_payload for a in g.attempted_by)

    if all(exhausted(g) for g in material):
        return ExitMode.CAPABILITY
    return ExitMode.BUDGET


def offers_continuation(mode: ExitMode) -> bool:
    """CAPABILITY must NEVER offer to try again.

    "Shall I try again?" when no tool can answer spends the user's patience on a
    guaranteed failure and implies the answer is reachable. That is worse than
    saying nothing.
    """
    return mode in (ExitMode.BUDGET, ExitMode.ERROR)
