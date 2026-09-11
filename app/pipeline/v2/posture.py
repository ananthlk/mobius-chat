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

from dataclasses import dataclass, field, replace
from enum import Enum


# ── constants ────────────────────────────────────────────────────────────────

STUCK_AGE_ROUNDS = 3          # [GUESS] fires on 182/837 multi-round turns
STUCK_MIN_LEVERS = 2          # [GUESS] distinct attempts before "change lever"
REPEAT_JACCARD = 0.70         # [GUESS] 24.5% of consecutive rag pairs exceed it
TREND_WINDOW = 2              # rounds compared to classify direction


# ── bootstrap round costs ────────────────────────────────────────────────────
#
# THESE ARE PRIORS, NOT PREDICTIONS. Their only job is to let v2 run at all.
# The A/B is the judge.
#
# Every number below describes V1: v1's tool selection, v1's 14,271-token
# manifest, v1's undifferentiated prompt, v1's round structure. v2 changes all
# four, so measuring v1 more precisely does not make these more predictive --
# it makes them a more precise description of the thing being replaced.
# turn_rounds overwrites each one with a real per-posture measurement as soon as
# v2 executes, and at that point these should be deleted, not refined.
#
# Measured 60d from thinking_log elapsed_s deltas, attributing each interval to
# the round that RAN it (elapsed_s is stamped at round START, so duration(n) =
# elapsed(n+1) - elapsed(n)):
#
#   healthcare_query      n=  45   p50 23.0   p90 35.4
#   search_corpus         n=  31        13.7       25.7
#   (no tool call)        n=1367        12.7       30.5
#   rag                   n=1691        10.4       31.0
#   web_scrape            n= 138         8.5       18.9
#   fetch_document        n= 362         7.8       18.1
#   appeals_get_playbook  n=  72         3.5        5.6
#   service_line_limits   n=  31         2.2        2.7
#
# CORRECTION 2026-09-11: an earlier version of this table was built on an
# off-by-one -- it attributed each interval to the NEXT round's tool, so "rag
# = 9.2s" actually measured the round that DECIDED to call rag. Ananth caught
# it by asking whether the cost of rag had been included or only react saying
# "call rag". The claim it produced -- "a rag round is cheaper than a round
# calling nothing" -- was an artifact and is withdrawn.
#
# WHAT SURVIVES, and is the load-bearing fact: the spread across tools is ~10x
# (23.0s to 2.2s). EXPLORE has no single cost -- it depends entirely on which
# tool the shortlist offers, which is Tool Manifest's estimate() and not a
# constant anyone can type here.

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
    Posture.FRAME.value:        RoundCost(11.7, 32.9, "BOOTSTRAP v1 no-tool round n=1367"),
    # the tool-calling round; rag is the dominant case at 876 of 1,481 tool rounds
    Posture.EXPLORE.value:      RoundCost(10.4, 31.0, "BOOTSTRAP v1 rag round n=1691 — real cost is per-tool, 2.2s..23.0s"),
    # scoping, no tool call
    Posture.NARROW.value:       RoundCost(11.7, 32.9, "BOOTSTRAP v1 no-tool round n=1367"),
    # generating routes, no tool call -- same shape as NARROW
    Posture.ALTERNATIVES.value: RoundCost(11.7, 32.9, "BOOTSTRAP v1 no-tool round n=1367"),
    # the critic, measured directly on llm_calls
    Posture.VALIDATE.value:     RoundCost(9.6, 16.9, "MEASURED integrator_critic n=135 (v1, unchanged by v2)"),
    # the final synthesis round
    Posture.COMMUNICATE.value:  RoundCost(10.0, 27.5, "BOOTSTRAP v1 no-tool round n=1367"),
}


def round_cost(posture: "Posture | str") -> RoundCost:
    """Cost of running one round in this posture.

    BOOTSTRAP values. See the table header: these describe v1 and exist only to
    let v2 run until turn_rounds measures v2. Do not tune them.

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
    # The promise's band (13±5, 31±8, 95±25). NOT merely a reporting tolerance:
    # it is the sanctioned overrun -- budget you may draw on WITH JUSTIFICATION
    # and a record, when you are close and converging.
    band_s: float = 0.0
    band_drawn_s: float = 0.0    # how much of it this turn has already spent


# The root gap. Ananth, 2026-09-11: "the question is the gap."
#
# Before any evidence exists, the USER'S QUESTION is the open gap -- it is
# exactly the thing not yet closed. Treating an empty gap list as "nothing worth
# spending on" was wrong for the same reason FRAME was wrong: a condition true
# for a reason unrelated to the decision. Empty means NOTHING HAS BEEN NAMED,
# not NOTHING IS NEEDED.
#
# Measured: production round 1 carries avg 0.46 gaps, because react only names a
# gap once evidence shows something missing. The question itself is never
# emitted as one -- it is implicit, and implicit is what the machine could not
# see.
ROOT_GAP_ID = "G0"


def seed_root_gap(question: str, *, round_index: int = 1) -> Gap:
    """The question, as the gap it is. Nothing attempted against it yet."""
    return Gap(
        gap_id=ROOT_GAP_ID,
        text=(question or "").strip() or "the user's question",
        opened_round=round_index,
        importance="high",     # it is the whole turn
        attempted_by=(),
    )


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
    # Seeded from the question when the ledger is empty -- see seed_root_gap.
    question: str = ""
    # Gap texts react reported CLOSED across the turn so far. Carried for the
    # RECORD, not for the decision: nothing in select() reads it. It is here
    # because "gaps closed" is the only recall measure this system has, and a
    # row that reports what was opened and never what was closed cannot show
    # whether a round bought anything.
    gaps_closed: tuple[str, ...] = ()


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
    # 0 -> 1 IS NOT A RISING TREND. It is the first gap being NAMED, which is
    # what a first round is for. Counting it as "still discovering" fired
    # trend_increasing on essentially every two-round turn in dev on
    # 2026-09-11 -- history [0, 1] -- and asked for another round on turns
    # that had just started to see their own question.
    #
    # A trend needs something to have been a trend FROM. Zero is not that.
    if prev == 0:
        return Trend.FLAT
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


def converging(state: RoundState) -> bool:
    """Is there EVIDENCE the next round closes it -- not a feeling that it will.

    Ananth: "would you relax a constraint like cost or latency when you are this
    close to a final answer, and you have a good feeling that one more round
    will close it."

    The tension is real. The answer cannot be "a good feeling", because
    self-reported confidence is the one signal produced by the thing being
    judged, and this seat has refused to let it authorise spend all week. So
    convergence has to be evidential:

      * few gaps open (a wide-open turn is not one round from done)
      * the gap count is NOT rising  (measured: rising -> 25.4% closure vs ~41%)
      * something CAME BACK on the last attempt -- the payload check, never the
        tool's own success flag

    All three, because each alone has a failure mode: one gap can be one that
    has resisted four rounds; a flat count can be a stalled turn; and a payload
    can be irrelevant.
    """
    if not state.open_gaps or len(state.open_gaps) > 2:
        return False
    if trend(state.gaps_open_history) is Trend.INCREASING:
        return False
    return any(
        a.returned_payload
        for g in state.open_gaps for a in g.attempted_by
    )


def may_overrun(state: RoundState) -> tuple[bool, str]:
    """May this turn spend INTO the band? Returns (allowed, why).

    THE PROMISE IS NOT EDITED. It stays frozen and the attestation still records
    delivered vs promised, so a turn that draws on the band shows as kept=False,
    in_band=True -- which is the honest shape: we exceeded what we promised, we
    stayed inside what we said the tolerance was, and we said why.

    Relaxing is asymmetric and the asymmetry is the point:
      * COST is invisible to the user. Relaxing it costs money, not trust.
      * LATENCY is what the user is sitting through. Relaxing it spends the
        thing the promise was about.
    So cost relaxes on convergence alone; latency additionally requires the
    overrun to be BOUNDED by the band and not already drawn.
    """
    if not converging(state):
        return False, "not converging: no evidence one more round closes it"
    need = round_cost(Posture.EXPLORE).p50_s
    left_in_band = max(0.0, state.budget.band_s - state.budget.band_drawn_s)
    if state.budget.remaining_s + left_in_band < need:
        return False, (
            f"even the band cannot fund it: need {need:.1f}s, "
            f"have {state.budget.remaining_s:.1f}s + {left_in_band:.1f}s band"
        )
    return True, (
        f"converging with {len(state.open_gaps)} gap(s) open and evidence "
        f"returning; drawing {need:.1f}s from a {left_in_band:.1f}s band"
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
    # The SAME state select() decided on -- seeded root gap included. Without
    # this, every row with an empty ledger reported gates computed on a state
    # the decision never saw.
    state = effective_state(state)
    buy = state.next_round_cost_s or round_cost(Posture.EXPLORE).p50_s
    act = state.acting_cost_s or round_cost(Posture.COMMUNICATE).p50_s
    return state.budget.remaining_s >= (buy + act)


def worth_spending(state: RoundState, *, allow_overrun: bool = False) -> Gap | None:
    floor = _IMPORTANCE_ORDER.get(state.min_importance, 1)
    if not spendable(state) and not allow_overrun:
        return None
    for gap in state.open_gaps:
        if _IMPORTANCE_ORDER.get(gap.importance, 1) < floor:
            continue
        if stuck(gap, state.round_index):
            continue
        return gap
    return None


def _stuck_count(state: RoundState) -> int:
    """Gaps that are UNREACHABLE — the same population alternatives_worth_it()
    admits, not a narrower one.

    It counted only stuck() — a gap that has resisted STUCK_AGE_ROUNDS — while
    alternatives_worth_it() also admits a gap that was attempted and returned
    nothing. So ALTERNATIVES fired on q12 of ab-e491c9a27a with the message
    "0 gap(s) unreachable: offer a route rather than a shortfall". A posture
    reporting zero instances of the thing it exists for.

    Two populations of one list, counted by two functions, disagreeing about
    the same state — the third instance of that shape today. One predicate,
    named, called by both."""
    return sum(1 for g in state.open_gaps if unreachable(g, state.round_index))


def unreachable(gap: Gap, current_round: int) -> bool:
    """A gap we cannot get to: resisted too long, OR tried and nothing came
    back. Not merely unbought — a gap nobody has attempted is UNFUNDED, and
    telling someone to go elsewhere for something we never tried is the
    flattering error this distinction exists to prevent."""
    return bool(
        stuck(gap, current_round)
        or (gap.attempted_by and not any(a.returned_payload for a in gap.attempted_by))
    )


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
    # The SAME predicate _stuck_count uses. They disagreed, and the posture
    # announced "0 gap(s) unreachable" while firing.
    return any(unreachable(g, state.round_index) for g in state.open_gaps)


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
    # True when this round is funded from the band rather than from the
    # promise. Recorded on turn_rounds so a breach can be told apart from a
    # DELIBERATE, evidenced overrun -- those look identical in a latency number
    # and are opposite facts about the governor.
    overran: bool = False
    # WHICH BRANCH of select() produced this. Not decoration: the same posture
    # comes out of different branches for opposite reasons, and a row that says
    # only "explore" cannot be argued with. Ananth, 2026-09-11: "I want to know
    # the rationale for why v2 made the decision it did."
    branch: str = ""



def effective_state(state: "RoundState") -> "RoundState":
    """The state the decision is ACTUALLY made on.

    "The question is the gap." An empty ledger is an UNNAMED gap, not an absent
    one, so the root gap stands in until react names something specific.

    This was inline at the top of select(), which meant select() decided on a
    SEEDED state while explain() reported the UNSEEDED one it was handed. The
    rows showed `open gaps 0` and `worth_spending None` beside
    `branch=gap_affordable` and `because="closing G0"` -- a gap the gap list did
    not contain. The emit was faithful to its input and wrong about the
    decision, which is worse than no emit: it invites you to argue with a state
    that never decided anything.

    One function, called by both, so the two cannot diverge again.
    """
    if not state.open_gaps and state.question:
        root = seed_root_gap(state.question, round_index=state.round_index)
        return replace(state, open_gaps=(root,))
    return state


def select(state: RoundState) -> Decision:
    """One posture per round. Skipping is what the tier IS."""
    if state.errored:
        return Decision(Posture.COMMUNICATE, "errored: one exit for every path",
                        branch="errored")

    # FRAME IS DELIBERATELY UNREACHABLE. Removed 2026-09-11.
    #
    # It fired on `not open_gaps and round_index <= 1` -- which is POSITIONAL,
    # the exact shape criticised in is_guidance_round. FRAME is supposed to mean
    # "we are answering the wrong question"; what it actually meant was "this is
    # round one". Those coincide on round 1 and diverge everywhere else, and in
    # the shadow it produced 50 of 88 divergences (57%) -- every one at round 1,
    # against a v1 that has no FRAME concept and says `search`.
    #
    # A posture whose trigger is a round number is not a posture, it is a label
    # for a position. Round 1 with no gaps is EXPLORE/DISCOVER: we are
    # gathering, which is what v1 says too.
    #
    # The posture stays DEFINED because the job is real -- a wrong frame makes
    # every subsequent round waste money, and it is the cheapest failure to fix
    # and the most expensive to miss. It stays UNFIRED because there is no
    # signal for it yet. Low confidence does not mean it: a model can be
    # confidently on the wrong question. Candidate tells, none built: gaps that
    # keep reopening, or evidence that arrives relevant-but-not-responsive.
    #
    # Declared-unreachable is not the dead-terminal defect. That terminal was
    # unreachable by accident, with a docstring and three green tests vouching
    # for it. This one is unreachable on purpose, says so, and has a test
    # asserting select() never returns it -- so it cannot come back silently.

    if trend(state.gaps_open_history) is Trend.INCREASING and spendable(state):
        # `and spendable(state)` added 2026-09-11. This branch returned
        # "buy another round" BEFORE the budget was consulted -- every other
        # spending branch reserves buy+act, this one reserved nothing. Live on
        # q12/ab-c311023248 it asked for a round with 0.0s remaining and a
        # 20.4s shortfall, and only the exit mode stopped it. The exit mode
        # doing all the work is what kept it invisible.
        #
        # Same shape as the executor runaway, one level up: discovering that
        # you are lost is not a reason you can afford to keep walking.
        return Decision(
            Posture.EXPLORE,
            "gaps increasing: still discovering, closure is 25.4% here",
            directive=Directive.DISCOVER,
            # 🔴 THIS BRANCH DOES NOT CHECK spendable(). It returns "buy
            # another round" before the budget is consulted -- the same shape
            # as the runaway. It is named here so every row it produces says
            # so; see BRANCH_SKIPS_BUDGET.
            branch="trend_increasing",
        )

    # "The question is the gap." An empty ledger is an UNNAMED gap, not an
    # absent one -- so the root gap stands in until react names something
    # specific. Without this the machine said COMMUNICATE on round 1 before it
    # had looked at anything: 50 of 87 divergences in the 2026-09-11 sample.
    state = effective_state(state)

    gap = worth_spending(state)
    overran = False
    if gap is None and state.open_gaps:
        # Out of promise budget. Ananth's question: would you relax a
        # constraint when you are this close and one more round would close it?
        # Yes -- but only on EVIDENCE of convergence, only into the band, and
        # only on the record. Never on a feeling.
        allowed, why = may_overrun(state)
        if allowed:
            gap = worth_spending(state, allow_overrun=True)
            if gap is not None:
                return Decision(
                    Posture.EXPLORE,
                    f"OVERRUN: closing {gap.gap_id} — {why}",
                    directive=Directive.CLOSE,
                    gap_targeted=gap.gap_id,
                    prior_queries=tuple(a.query for a in gap.attempted_by if a.query),
                    overran=True,
                    branch="overrun_into_band",
                )
    if gap is not None:
        return Decision(
            Posture.EXPLORE,
            f"closing {gap.gap_id} (open {gap.age(state.round_index)} rounds, "
            f"{distinct_levers(gap)} levers spent)",
            directive=Directive.CLOSE,
            gap_targeted=gap.gap_id,
            prior_queries=tuple(a.query for a in gap.attempted_by if a.query),
            branch="gap_affordable",
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
                branch="gaps_unreachable",
            )
        return Decision(
            Posture.NARROW,
            "gaps open but none worth buying: scope and name what is left",
            branch="nothing_worth_buying",
        )

    if validate_worth_it(state):
        return Decision(Posture.VALIDATE, "quality uncertain and a fix round is affordable",
                        branch="validate_affordable")

    return Decision(Posture.COMMUNICATE, "no gap worth spending on",
                    branch="no_gap_worth_spending")


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


def explain(state: "RoundState", decision: "Decision") -> dict:
    """THE DECISION, SHOWN — every input and every intermediate, not a verdict.

    Ananth, 2026-09-11, after seeing a round row that said only "gaps open but
    none worth buying":

        "I can make determinations without the question, the answer, the state
        at that point... round by round what was discovered or not. That is the
        only way to tweak v2. I want to know the RATIONALE for why v2 made the
        decision it did. This is the emit that will lead us to understand."

    He is right, and the row was worse than thin: `rationale` asserted "gaps
    open" while the row's own `gaps_opened` column was `[]`, because compare()
    never emitted it. A conclusion with no inputs cannot be argued with, and a
    decision you cannot argue with cannot be tuned.

    PURE, and it recomputes NOTHING it does not have to: every field here is
    either read off the state or produced by calling the same predicate select()
    called. If this disagreed with select(), it would be a second author of the
    decision -- so it calls, never reimplements.
    """
    # The SAME state select() decided on -- seeded root gap included. Without
    # this, every row with an empty ledger reported gates computed on a state
    # the decision never saw.
    state = effective_state(state)
    buy = state.next_round_cost_s or round_cost(Posture.EXPLORE).p50_s
    act = state.acting_cost_s or round_cost(Posture.COMMUNICATE).p50_s
    can_spend = spendable(state)
    gap = worth_spending(state)
    over_ok, over_why = may_overrun(state)

    return {
        # ── what was true when the decision was made ────────────────────────
        "round": state.round_index,
        "open_gaps": [
            {
                "id": g.gap_id,
                "text": (g.text or "")[:160],
                "opened_round": g.opened_round,
                "age_rounds": g.age(state.round_index),
                "levers_spent": distinct_levers(g),
                "attempts": [
                    {"round": a.round_index, "tool": a.tool,
                     "query": (a.query or "")[:80],
                     # THE payload check, never the tool's own success flag --
                     # a tool can succeed and return nothing.
                     "returned_payload": a.returned_payload}
                    for a in g.attempted_by
                ],
                # WHY this gap was passed over, when it was.
                "stuck": stuck(g, state.round_index),
            }
            for g in state.open_gaps
        ],
        "gaps_open_history": list(state.gaps_open_history),
        "gaps_closed": list(state.gaps_closed),
        "trend": trend(state.gaps_open_history).value,

        # ── the arithmetic that actually decided it ─────────────────────────
        "budget": {
            "remaining_s": round(state.budget.remaining_s, 2),
            "next_round_costs_s": round(buy, 2),
            "acting_costs_s": round(act, 2),
            "needs_s": round(buy + act, 2),
            # Reserve the cost of ACTING, not just of looking: buying the last
            # round to DISCOVER a problem leaves nothing to fix it with.
            "spendable": can_spend,
            "shortfall_s": (None if can_spend
                            else round((buy + act) - state.budget.remaining_s, 2)),
        },

        # ── the branch taken, named ─────────────────────────────────────────
        "worth_spending": (gap.gap_id if gap else None),
        "converging": converging(state),
        "may_overrun": {"allowed": over_ok, "why": over_why},
        "alternatives_worth_it": alternatives_worth_it(state),
        "validate_worth_it": validate_worth_it(state),
        "unreachable_gaps": _stuck_count(state),
        "exit_mode": exit_mode(state).value,

        # ── and only then, the conclusion ───────────────────────────────────
        "branch": decision.branch,
        "posture": decision.posture.value,
        "because": decision.because,
        "gap_targeted": decision.gap_targeted,
        "overran": decision.overran,
    }


# 🔴 FILED 2026-09-11, surfaced by explain() on its first run.
#
# The `trend_increasing` branch returns EXPLORE -- "buy another round" -- BEFORE
# spendable() is consulted. Every other spending branch reserves the cost of
# ACTING as well as looking; this one reserves nothing. It is the same shape as
# the executor runaway, one level up: a branch that can spend without an
# affordability check.
#
# Observed live on q12 of run ab-c311023248: posture=explore, worth_spending=
# null, budget shortfall 12.4s, and the EXIT MODE had to override it to
# finalize. The override worked -- which is exactly why this was invisible
# until the inputs were emitted.
#
# NOT fixed here. Moving it changes what every shadow row since 2026-09-11 has
# meant, so it needs its own re-derivation against the recorded inputs -- which
# is now possible for the first time, because those inputs are persisted.
# FIXED 2026-09-11 — `and spendable(state)` on the branch, and trend() no
# longer reads 0 -> 1 as rising. Kept as a named record because the rows
# recorded BEFORE this change were produced by the unguarded branch and must
# not be pooled with rows after it.
BRANCH_SKIPS_BUDGET = ("FIXED 2026-09-11: trend_increasing now requires "
                       "spendable(); rows before this commit came from the "
                       "unguarded branch and are a different population")
