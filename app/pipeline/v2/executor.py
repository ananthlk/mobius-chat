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
_POSTURE_TO_DIRECTIVE = {
    # Keep gathering. Evidence is thin and a gap is worth spending on.
    Posture.EXPLORE: EXTEND,
    # Remediation: the critique named unsupported claims and the next round is
    # aimed at them. Same action as EXPLORE at this branch, different reason --
    # and react_loop injects the critique either way.
    Posture.NARROW: EXTEND,
    # Run the check again. At THIS branch the groundedness floor has already
    # run, so VALIDATE means "that verdict was not good enough, go again".
    Posture.VALIDATE: EXTEND,
    # Give the person somewhere to go instead of "I don't know".
    Posture.ALTERNATIVES: EXTEND,
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
# That is right for NARROW (aimed at named claims) and defensible for VALIDATE.
# It is wrong for EXPLORE -- a gathering round gets a remediation prompt -- and
# it is wrong for ALTERNATIVES, which wants a reframe prompt that does not
# exist in v1 at all.
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
    Posture.ALTERNATIVES: "reframe round receives the remediation prompt; v1 "
                          "has no alternatives prompt at all",
}


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


def decide(decision: Decision, exit: ExitMode | None = None) -> Action:
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
        prompt_mismatch=PROMPT_MISMATCH.get(posture) if directive == EXTEND else None,
    )
