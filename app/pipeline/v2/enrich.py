"""Should the critic and next-steps run for this answer? Decided, and recorded.

Ananth, 2026-09-12: "the question is based on some criteria you still decide if
we need to run them or not.. for now assume always".

ALWAYS, BUT NOT BLINDLY. `ALWAYS_ENRICH` is True and the answer is yes today --
but the criteria are EVALUATED and RECORDED on every turn anyway, so when there
is data to set thresholds we are tuning a function that already exists instead
of retrofitting one under time pressure. Same pattern as the next_round_worth_it
ack: observe now, gate later, and let the disagreement teach us the threshold.

Hard-coding `return True` would have thrown away exactly the rows needed to
stop doing that.

ONE PREREQUISITE SURVIVES "ALWAYS": there must be an answer. Critiquing nothing
is not a policy choice, it is a wasted call against an empty string -- so it is
a PREREQUISITE, reported separately from the criteria, and it is why
`run_critic` can still be False while ALWAYS_ENRICH is True.

WHY THIS IS NOT A REACT ROUND. An in-loop round re-carries the accumulated
context: measured in production, round 2 costs 3.26x round 1 in dollars and
2.82x in latency, because it pays for round 1 again. A separate critic call
needs only the answer, the facts with provenance, and the open gaps -- roughly
2-5k tokens against ~36k. Critic and next-steps also read the same inputs and
depend on neither, so out here they run concurrently; in-loop they would be two
serial rounds, or one round doing both jobs, which is the self-review
blocks.py refuses.

PURE. Facts in, decision out. No I/O, no calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Ananth, 2026-09-12: "for now assume always". Env-overridable so the switch
# does not need a deploy, and named so the default is visible at a glance.
ALWAYS_ENRICH = (os.environ.get("MOBIUS_V2_ENRICH_ALWAYS", "1").strip()
                 not in ("0", "false", "no", ""))

# Reopening is a different decision from enriching, and it stays strict: a
# round is only bought back when the critic finds something OBJECTIVELY
# unfinished. "Could be better" is unbounded and would spend every second of
# every budget.
REOPEN_ON = ("not_attempted",)


@dataclass(frozen=True)
class EnrichDecision:
    run_critic: bool = False
    run_next_steps: bool = False
    # Why, in one clause, for the trace and for the row.
    why: str = ""
    # What was OBSERVED, whether or not it gated anything today. These are the
    # rows that will eventually set the thresholds.
    criteria: dict = field(default_factory=dict)
    # Could a reopen even be afforded if the critic asks for one? Ours to
    # answer (budget, rounds), never the model's.
    reopen_affordable: bool = False

    @property
    def runs_anything(self) -> bool:
        return self.run_critic or self.run_next_steps


def should_enrich(*, answer: str, facts=(), open_gaps=(), is_complete=None,
           elapsed_s: float | None = None, promise_s: float | None = None,
           rounds_left: int = 0, round_cost_s: float | None = None) -> EnrichDecision:
    """Run the critic and next-steps for this answer?

    NAMED should_enrich, not decide: posture.decide() already exists in this
    module family and carries a hard requirement that every call site supply
    `affordable`. Two functions named decide() cannot be told apart by an AST
    gate matching on attribute name -- mine tripped that gate immediately --
    and, more to the point, cannot be told apart by a person reading a call
    site either.

    Today: yes, whenever there is an answer. The criteria below are measured
    and carried so the "yes" can become a judgement later.
    """
    answer = (answer or "").strip()
    ungrounded = sum(1 for f in facts or ()
                     if not getattr(f, "grounded", False))
    grounded = sum(1 for f in facts or () if getattr(f, "grounded", False))

    # AFFORDABILITY IS OURS. The critic may ask to reopen; whether we can pay
    # for it is a budget fact, and asking the model to respect a budget it
    # cannot see would be asking it to guess.
    affordable = False
    if rounds_left > 0 and elapsed_s is not None and promise_s is not None:
        cost = round_cost_s if round_cost_s is not None else 0.0
        affordable = (elapsed_s + cost) <= promise_s

    criteria = {
        # Each of these is a candidate threshold, recorded before it gates.
        "has_answer": bool(answer),
        "answer_chars": len(answer),
        "open_gaps": len(open_gaps or ()),
        "grounded_facts": grounded,
        # The strongest single signal we have for "this needs checking": the
        # answer carries claims nothing supports.
        "ungrounded_facts": ungrounded,
        # react saying complete WHILE gaps are open is the disagreement the
        # whole compliance surface exists to catch, and it is exactly when a
        # second opinion is worth paying for.
        "claims_complete_with_gaps_open": bool(is_complete) and bool(open_gaps),
        "elapsed_s": elapsed_s,
        "promise_s": promise_s,
        "over_promise": (None if (elapsed_s is None or promise_s is None)
                         else elapsed_s > promise_s),
        "rounds_left": rounds_left,
    }

    if not answer:
        # NOT a policy decision. There is nothing to critique, and a call
        # against an empty string returns confident prose about nothing.
        return EnrichDecision(False, False,
                              "no answer yet — nothing to critique",
                              criteria, affordable)

    if ALWAYS_ENRICH:
        return EnrichDecision(True, True, "always-on (criteria recorded, not gating)",
                              criteria, affordable)

    # The shape the gating version will take. Unreachable today, and kept
    # honest rather than left as a TODO: these are the conditions that would
    # justify the spend if we were paying attention to it.
    worth_it = (criteria["ungrounded_facts"] > 0
                or criteria["claims_complete_with_gaps_open"]
                or criteria["open_gaps"] > 0)
    return EnrichDecision(
        worth_it, worth_it,
        ("unsupported claims or unclosed gaps" if worth_it
         else "answer is grounded and complete — nothing to check"),
        criteria, affordable)


def should_reopen(critic_verdict: dict | None, decision: EnrichDecision) -> tuple[bool, str]:
    """After the critic: buy another round, or hand the decision to the user?

    Ananth: "if time permits we allow for a critic to reopen.. if time does not
    permit, we defer that to the user".

    Only on REOPEN_ON verdicts -- objectively unfinished work, never "this
    could be better", which has no floor. And only when we can afford it: an
    unaffordable reopen is not a refusal, it is the user's decision to make,
    and they can only make it if the answer says what is missing.
    """
    if not critic_verdict:
        return False, "no critic verdict"
    parts = critic_verdict.get("parts") or []
    unfinished = [p for p in parts
                  if isinstance(p, dict) and p.get("status") in REOPEN_ON]
    if not unfinished:
        return False, "nothing objectively unfinished"
    if not decision.reopen_affordable:
        return False, (f"{len(unfinished)} part(s) never attempted, but no "
                       "budget left — handing the choice to the user")
    return True, f"{len(unfinished)} part(s) never attempted and budget allows"
