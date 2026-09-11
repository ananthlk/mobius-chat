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


# ── measured round costs ─────────────────────────────────────────────────────
#
# Replaces a hardcoded 10.3s that priced every round identically. Measured over
# 60 days of production rounds, from elapsed_s deltas in thinking_log:
#
#   (no tool) mid round      n=1173   p50 11.7   p90 32.9   <- the MOST expensive
#   search_corpus            n=  33        11.4       25.0
#   (no tool) FINAL round    n=1301        10.0       27.5
#   web_scrape               n= 120         9.7       27.3
#   rag                      n= 876         9.2       26.4   <- CHEAPER than no tool
#   fetch_document           n= 325         8.9       20.8
#   appeals_get_playbook     n=  47         3.5       11.8
#
# THE FINDING THAT MATTERS: a round that calls rag is CHEAPER than a round that
# calls nothing. The dominant cost is the reasoning call, not the tool -- so the
# lever is round COUNT, not tool choice, and a better tool wins by removing a
# round rather than by being fast.
#
# 🔴 PROVENANCE: these are PROXIES. They are measured v1 round KINDS mapped onto
# postures, not measurements of postures -- postures do not exist in production
# yet, so no per-posture number can exist. turn_rounds replaces every one of
# these with a real measurement once v2 runs, and until then a cost here is a
# defensible estimate and not a fact. Do not quote them as per-posture costs.


class Posture(str, Enum):
    FRAME = "frame"
    EXPLORE = "explore"
    NARROW = "narrow"
    ALTERNATIVES = "alternatives"
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
class RoundCost:
    """p50 and p90. A range, never a point -- the queue wait proved a point
    estimate lies when the underlying is bimodal."""
    p50_s: float
    p90_s: float
    basis: str


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


# posture -> measured proxy. `basis` names WHICH measurement, so a reader can
# check whether the proxy still fits when a posture's behaviour changes.
_ROUND_COST: dict[str, RoundCost] = {
    # reasoning about the question, no tool call
    Posture.FRAME.value:        RoundCost(11.7, 32.9, "PROXY v1 (no tool) mid round n=1173"),
    # the tool-calling round; rag is the dominant case at 876 of 1,481 tool rounds
    Posture.EXPLORE.value:      RoundCost(9.2, 26.4, "PROXY v1 rag round n=876"),
    # scoping, no tool call
    Posture.NARROW.value:       RoundCost(11.7, 32.9, "PROXY v1 (no tool) mid round n=1173"),
    # generating routes, no tool call -- same shape as NARROW
    Posture.ALTERNATIVES.value: RoundCost(11.7, 32.9, "PROXY v1 (no tool) mid round n=1173"),
    # the critic, measured directly on llm_calls
    Posture.VALIDATE.value:     RoundCost(9.6, 16.9, "MEASURED integrator_critic n=135"),
    # the final synthesis round
    Posture.COMMUNICATE.value:  RoundCost(10.0, 27.5, "PROXY v1 (no tool) FINAL round n=1301"),
}


def round_cost(posture: "Posture | str") -> RoundCost:
    """Cost of running one round in this posture.

    Raises on an unknown posture rather than defaulting. A default would price a
    new posture at someone else's number and never say so -- the silent-default
    shape this program has spent a week removing. A posture with no cost is a
    posture that cannot be budgeted, and that must be loud.
    """
    key = posture.value if isinstance(posture, Posture) else str(posture)
    try:
        return _ROUND_COST[key]
    except KeyError:
        raise KeyError(
            f"no measured round cost for posture {key!r} -- add one to "
            f"_ROUND_COST with its basis before budgeting it"
        ) from None


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
    alternatives_cost_s: float = 0.0
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


def cost_of(posture: Posture, state: RoundState) -> float:
    """What buying this posture costs, in seconds. Prefers the measured table
    over whatever the caller guessed."""
    explicit = {
        Posture.VALIDATE: state.validate_cost_s,
        Posture.ALTERNATIVES: state.alternatives_cost_s,
    }.get(posture)
    if explicit:
        return explicit
    return round_cost(posture).p50_s


def spendable(state: RoundState) -> bool:
    """Reserve the cost of ACTING, not just the cost of looking.

    Buying the last round of budget to DISCOVER a problem leaves nothing to fix
    it with. Cost is deliberately absent from this check: react_1 reports cost on
    686 of 1,236 calls, so spend is understated on precisely the path the
    governor steers, and an understated spend FAILS OPEN -- it permits rounds it
    should refuse. Cost may INFORM a decision; it must not DECIDE one. Time can
    decide; the promise clock is complete.
    """
    # An EXPLORE round is what "one more round" buys, and acting on what it
    # finds costs a round too -- reserve BOTH. next_round_cost_s, when the
    # caller supplied one, wins over the table so a tier-specific measurement
    # can override the global proxy.
    buy = state.next_round_cost_s or round_cost(Posture.EXPLORE).p50_s
    act = state.acting_cost_s or round_cost(Posture.COMMUNICATE).p50_s
    return state.budget.remaining_s >= (buy + act)


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


def _stuck_count(state: RoundState) -> int:
    return sum(1 for g in state.open_gaps if stuck(g, state.round_index))


def alternatives_worth_it(state: RoundState) -> bool:
    """Offer a route when a gap is unreachable -- not when it is merely unbought.

    Requires (a) at least one gap that is genuinely stuck or has been attempted
    and returned nothing, and (b) budget for the round. A gap nobody has tried
    yet is not unreachable; it is unfunded, and the honest thing there is to say
    we ran out of time, not to suggest the user go elsewhere.

    That distinction is the same one that separates BUDGET from CAPABILITY at
    exit, and getting it wrong in the flattering direction -- offering
    alternatives for something we simply did not attempt -- would tell the user
    to go away when we could have answered.
    """
    if not state.open_gaps:
        return False
    if state.budget.remaining_s < cost_of(Posture.ALTERNATIVES, state):
        return False
    return any(
        stuck(g, state.round_index)
        or (g.attempted_by and not any(a.returned_payload for a in g.attempted_by))
        for g in state.open_gaps
    )


def validate_worth_it(state: RoundState) -> bool:
    """Value of information: spend on knowing only when knowing can change doing.

    Never a mode test. `agentic` was a proxy for expected difficulty, which is a
    forecast -- replace it with the forecast, not another proxy. A verdict with
    nowhere to go is pure cost, so the fix round is reserved, not just the look.
    """
    affordable = state.budget.remaining_s >= (
        cost_of(Posture.VALIDATE, state) + (state.acting_cost_s or round_cost(Posture.EXPLORE).p50_s)
    )
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
        # NARROW scopes; ALTERNATIVES makes the shortfall actionable. Ananth,
        # 2026-09-11: "when stuck we should have a posture of
        # alternatives/reframe before communicate -- this leaves the user with
        # something they can get without saying I don't know; some help as to
        # where they can go."
        #
        # The distinction matters because they answer different questions:
        #   NARROW        -- what are we claiming, and what is left open
        #   ALTERNATIVES  -- given that it is open and we cannot reach it,
        #                    what CAN the user do
        #
        # Why it sits AFTER narrow and BEFORE communicate: it needs to know
        # what is being left open (narrow decides that), and its output is part
        # of what gets said (communicate delivers it).
        #
        # And it is most valuable exactly where the system is least able to
        # help: a CAPABILITY exit says "I have no source for x", which is
        # honest and useless on its own. attempted_by is what makes the
        # alternative specific rather than generic -- three failed rag attempts
        # says "our corpus does not carry this", which points somewhere.
        if alternatives_worth_it(state):
            return Decision(
                Posture.ALTERNATIVES,
                f"{_stuck_count(state)} gap(s) unreachable: offer a route rather "
                f"than a shortfall",
            )
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
