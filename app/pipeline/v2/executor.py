"""Step 2 — the narrow v2 executor. The decision varies; nothing else does.

WHAT THIS IS NOT: a second orchestrator. v2 does not re-implement the round
loop, the tool calls, the prompt composition or the publish path. react_loop.py
is 6,451 lines and its first port hazard is that `max_it` grows mid-turn, so a
`range()` loop silently drops every extension AND THE TESTS PASS. A rewrite
loses things no author finds in their own code.

WHAT IT IS: the same loop, with ONE substitution. At the point where v1 asks
"the model proposes it is done -- do we stop, extend, or ship with a warning?",
the posture machine answers instead of `governor.evaluate()`. Everything around
it is byte-identical because it is literally the same code path.

That is not a convenience. It is the experiment design:

    varied         the decision
    held constant  prompts, manifest, tools, model, publish, renderer,
                   the question, the tier, the traffic

Concurrent arms over the same traffic. A turn is routed to ONE arm and is never
handled by both -- two orchestrators sharing write state is the two-writer
defect at maximum scale.

PURE. Like posture.py: no clock, no DB, no env read at call time. The call site
does the effecting; this module only decides. That is what makes an arm
replayable from its stored row.

────────────────────────────────────────────────────────────────────────────
THE SURFACE, AND WHY IT IS THIS SMALL

At react_loop.py:5591-5624 the directive can only be three things, and each is
a real action the loop already knows how to take:

    extend     loop another round, with the critique injected as an observation
    finalize   ship it, appending the groundedness notice
    complete   fall through and publish

Those three ARE the promise. Rounds are latency, latency is cost, and this is
the branch that decides how many there are. A first executor that owns only
this branch still owns the whole quantity being compared.

The pre-round directive (search/consolidate) is NOT taken here. It selects a
prompt profile, and taking it without also owning prompt selection would vary
two things at once.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.pipeline.v2.posture import Decision, ExitMode, Posture

# v1's vocabulary at this branch. Named rather than inlined so a change in
# react_loop is a failing test here, not a silent no-op.
EXTEND = "extend"
FINALIZE = "finalize"
COMPLETE = "complete"

V1_DIRECTIVES = (EXTEND, FINALIZE, COMPLETE)


# ── the inverse map ─────────────────────────────────────────────────────────
#
# shadow.map_directive goes v1 -> v2 for OBSERVATION. This goes v2 -> v1 for
# EXECUTION, and it is deliberately NOT its inverse:
#
#   * map_directive is allowed to return None (unmapped) -- an observation may
#     decline to classify. An executor may not: every posture must produce an
#     action, because the turn is running and something has to happen next.
#   * map_directive splits `extend` by reason. Going the other way, several
#     postures legitimately collapse onto `extend` -- the collapse is lossy in
#     the same direction v1's own is, and that loss is RECORDED (see
#     PROMPT_MISMATCH) rather than left to be discovered.
#
#
# 🔴 CORRECTED 2026-09-11 AFTER A LIVE RUNAWAY: 98 rounds on one turn, v1
# saying `finalize` and v2 overriding it to `extend` every single round.
#
# The first version of this table mapped POSTURE NAMES. NARROW sounds like
# remediation, so I mapped it to `extend`. But posture.py returns NARROW from
# exactly ONE branch -- the one where `worth_spending()` already returned None
# -- and its own docstring says what it means there:
#
#     NARROW        -- what are we claiming, and what is left open
#     ALTERNATIVES  -- given that it is open and we cannot reach it,
#                      what CAN the user do
#
# Both are WRAP-UP postures, reached only when nothing is worth buying. Mapping
# either to "buy another round" inverts the decision it just made. Read the
# BRANCH that returns a posture, never the noun.
#
_POSTURE_TO_DIRECTIVE = {
    # The ONLY posture that means "buy another round": returned when
    # worth_spending() found a gap it can afford. The affordability check has
    # already happened inside select().
    Posture.EXPLORE: EXTEND,
    # A fix round, and select() only returns this when validate_worth_it() says
    # it is affordable. Genuinely another round.
    Posture.VALIDATE: EXTEND,
    # WRAP-UP. "gaps open but none worth buying: scope and name what is left."
    # Nothing is worth buying -- that is how this branch was reached.
    Posture.NARROW: COMPLETE,
    # WRAP-UP. Its output is part of what gets SAID, not a round of its own:
    # posture.py, "its output is part of what gets said (communicate delivers
    # it)". See ALTERNATIVES_NOT_RENDERED below for what v1 loses here.
    Posture.ALTERNATIVES: COMPLETE,
    # Done deciding. Whether it ships clean or with a notice is the exit mode's
    # call, not the posture's -- see below.
    Posture.COMMUNICATE: COMPLETE,
    # FRAME is unreachable by construction (posture.py:468) and a test asserts
    # select() never returns it. It is absent here on purpose: adding a row for
    # it would make the unreachability claim untestable from this side.
}


# 🔴 WHERE THIS EXECUTOR IS KNOWINGLY WRONG, stated before it ships.
#
# `extend` at this branch injects the CRITIQUE as the next round's observation.
# That is defensible for VALIDATE -- a fix round wants the critique. It is
# wrong for EXPLORE, where a GATHERING round receives a remediation prompt.
#
# (This block used to also name NARROW and ALTERNATIVES. It no longer does,
# because after the runaway neither extends at all -- see the corrected map.)
#
# This is the SAME defect the chat seat reported in v1's pre-round extend, and
# I am reproducing it rather than fixing it, deliberately: fixing it here would
# mean varying the prompt, and then a divergence could not be attributed to the
# decision. It is a known-wrong baseline, and a baseline you have written down
# is worth more than one you have quietly improved.
#
# Recorded per round so the comparison can separate "v2 chose badly" from "v2
# chose well and got the wrong prompt for it".
PROMPT_MISMATCH = {
    Posture.EXPLORE: "gathering round receives the remediation prompt (v1 has "
                     "no separate extend-to-gather prompt at this branch)",
}

# 🔴 AND WHERE IT IS KNOWINGLY LOSSY, which is a different failure.
#
# ALTERNATIVES now ships rather than extends -- correct, it is a wrap-up. But
# v1 has NO alternatives prompt, so the routes it decided to offer are never
# generated and never rendered. The posture is chosen and its output does not
# exist.
#
# That is the worst shape in this program's catalogue: an instruction whose
# output nothing reads. It is recorded on the row so the comparison cannot
# quietly credit v2 with an answer the person never saw. It is ALSO the
# concrete thing step 3 has to buy -- an alternatives prompt is the first
# per-module arm with a reason already measured for it.
ALTERNATIVES_NOT_RENDERED = (
    "v2 chose ALTERNATIVES; v1 has no alternatives prompt, so the routes were "
    "decided and never generated -- the person saw a normal answer"
)


@dataclass(frozen=True)
class Action:
    """What the loop should do, and everything needed to judge it later."""
    directive: str
    posture: Posture
    because: str
    exit_mode: ExitMode | None = None
    overran: bool = False
    gap_targeted: str | None = None
    # None when the prompt this round will receive matches the posture that
    # asked for it. A string when it does not -- see PROMPT_MISMATCH.
    prompt_mismatch: str | None = None

    @property
    def continues(self) -> bool:
        return self.directive == EXTEND


# ── THE CEILING ─────────────────────────────────────────────────────────────
#
# v1's `max_it` GROWS BY ONE on every extend. There is no natural stop: an
# executor that can say `extend` can say it forever, and on 2026-09-11 mine did
# -- 98 rounds on one turn, 420s, against a 31s promise.
#
# The corrected posture map removes the cause. This removes the CLASS. A
# decision module that can spend without limit is not bounded by the promise,
# and my own charter says bound, never choose. The bound must live where the
# decision is made, not in the thing being decided about -- react_loop cannot
# stop me, because I am the one telling it to continue.
#
# 6 is not a tuned number. It is above the highest round count observed in
# 1,367 v1 turns (4) and far below a runaway, and it exists to make the failure
# mode impossible rather than unlikely. It is a [GUESS] and is labelled one.
MAX_V2_EXTENSIONS = 6   # [GUESS] -- a fuse, not a target


def decide(decision: Decision, exit: ExitMode | None = None,
           extensions_used: int = 0) -> Action:
    """Posture -> the action v1's loop already knows how to take.

    `exit` splits COMMUNICATE, and only COMMUNICATE:

        COMPLETE            -> complete   ship it clean
        BUDGET / ERROR      -> finalize   ship it WITH the groundedness notice
        CAPABILITY          -> complete   ship it clean

    CAPABILITY is the one that matters and the one that is easy to get wrong.
    It means the answer could not be reached with the tools available -- not
    that the answer is ungrounded. Appending "these claims could not be
    verified" to it would attach a quality warning to a scope limit, and worse,
    v1's finalize path is also the path that offers continuation. A CAPABILITY
    exit MUST NEVER OFFER CONTINUATION: inviting someone to wait longer for an
    answer no amount of waiting produces is the cruelest thing this system can
    do, and it is one enum row away at all times.
    """
    posture = decision.posture
    directive = _POSTURE_TO_DIRECTIVE.get(posture)
    if directive is None:
        # Not a silent default. An unmapped posture reaching an executor means
        # the posture machine grew a state and this table did not; shipping the
        # turn is right, hiding it is not.
        return Action(
            directive=COMPLETE, posture=posture,
            because=f"UNMAPPED POSTURE {posture.value!r} -- shipped rather than "
                    f"stalled; the executor's table is behind the machine",
            exit_mode=exit, overran=decision.overran,
            gap_targeted=decision.gap_targeted,
        )

    # ── THE EXIT MODE DOMINATES EVERY POSTURE, not just COMMUNICATE ─────────
    #
    # Found by printing the whole table rather than by reading the code: with
    # the split applied only to COMPLETE, `EXPLORE + BUDGET` produced `extend`
    # -- spending a round the budget says does not exist -- and
    # `EXPLORE + CAPABILITY` produced `extend`, chasing an answer no tool can
    # reach. Both are the worst possible action for their state.
    #
    # "select() would never return EXPLORE when the budget is gone" is true
    # today and is EXACTLY the reasoning that produced FRAME: a guarantee held
    # somewhere else, relied on here, with nothing asserting it. A module must
    # be safe on its own inputs.
    #
    # When the posture and the exit contradict each other, STOPPING WINS.
    # Spending a round you cannot afford is unrecoverable; stopping a round
    # early is recoverable -- the person can ask again. The contradiction is a
    # disagreement inside my own module and it is recorded, not resolved
    # silently.
    contradiction = ""

    # The fuse, checked BEFORE the exit mode so it holds even when the exit
    # mode is wrong -- which is exactly the case that produced the runaway:
    # select() counted every open gap and returned NARROW, exit_mode() counted
    # only MATERIAL gaps and returned COMPLETE, and nothing stopped the loop.
    # Two populations in one module (see POPULATION_MISMATCH).
    if directive == EXTEND and extensions_used >= MAX_V2_EXTENSIONS:
        return Action(
            directive=FINALIZE, posture=posture,
            because=(f"{decision.because} [CEILING: {extensions_used} extensions "
                     f"already spent, max {MAX_V2_EXTENSIONS} -- stopped by the "
                     f"fuse, not by the decision]"),
            exit_mode=exit, overran=decision.overran,
            gap_targeted=decision.gap_targeted,
        )

    if exit in (ExitMode.BUDGET, ExitMode.ERROR, ExitMode.CAPABILITY):
        if directive == EXTEND:
            contradiction = (f" [posture {posture.value} wanted another round; "
                             f"exit {exit.value} overrode it -- stopping wins]")
        # BUDGET/ERROR ship WITH the groundedness notice; CAPABILITY ships
        # clean, because its limit is scope and not quality. See the docstring.
        directive = COMPLETE if exit is ExitMode.CAPABILITY else FINALIZE

    return Action(
        directive=directive,
        posture=posture,
        because=decision.because + contradiction,
        exit_mode=exit,
        overran=decision.overran,
        gap_targeted=decision.gap_targeted,
        prompt_mismatch=(PROMPT_MISMATCH.get(posture) if directive == EXTEND
                         else (ALTERNATIVES_NOT_RENDERED
                               if posture is Posture.ALTERNATIVES else None)),
    )


# ── FILED, NOT FIXED HERE ───────────────────────────────────────────────────
#
# select() and exit_mode() count DIFFERENT POPULATIONS of the same gap list:
#
#   select()     `if state.open_gaps:`          -- EVERY open gap
#   exit_mode()  material = [g for g in ... if importance >= floor]
#                                               -- only gaps above the floor
#
# So a turn with only below-floor gaps gets NARROW ("gaps open") from one and
# COMPLETE ("nothing material") from the other, about the same state, in the
# same module, on the same round. That is what let the runaway run: the fuse
# now catches it, but the disagreement is still there and it is a decision-core
# defect, not a wiring one.
#
# It is NOT fixed in this change, deliberately. The right fix chooses which
# population is correct -- and that choice changes what every recorded exit
# mode has meant since the shadow started, so it is its own change with its own
# re-derivation, not a line edited while stopping a runaway. Written down here
# because an objection you agree with and do not act on is worse than one you
# argue with, and the least I can do is make it impossible to rediscover.
POPULATION_MISMATCH = (
    "select() counts all open_gaps; exit_mode() counts only gaps at or above "
    "min_importance. Same state, same round, two answers. Filed 2026-09-11."
)
