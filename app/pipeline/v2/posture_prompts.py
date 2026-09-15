"""One prompt per posture — the thing this loop actually owns.

All six postures previously collapsed onto v1's three agent roles
(react_explore / synthesize / draft), so three of the six distinctions died at
the prompt boundary. The governor's whole job is choosing a posture; if the
model cannot tell which one it is in, the choice is decoration.

PROVENANCE. FRAME, EXPLORE, NARROW and ALTERNATIVES take their structure from
deep-research's planner prompt (scripts/research/planner.py), adopted rather
than paraphrased after a direct exchange with that seat on 2026-09-15.
COMMUNICATE is held pending the Deterministic UX seat, because it produces
what a person reads and a prompt that fights the renderer is worse than none.

WHAT IS DELIBERATELY NOT HERE. No formatting instructions. No "be concise, be
helpful". No tone. Those belong to the composition layer and the renderer; a
posture prompt says what the ROUND is for.
"""

from __future__ import annotations

from app.pipeline.v2.posture import Posture

# ── FRAME ───────────────────────────────────────────────────────────────────
# deep-research's `survey` object, which sits ABOVE the plans and is emitted
# before any plan exists. Their two highest-value fields are kept and the
# reason each earns its place is written down:
#
#   tools_i_passed_over      turns "it never occurred to me" and "I considered
#                            and rejected it" into different VISIBLE states.
#   what_i_expect_to_be_hard written BEFORE planning, so it is a falsifiable
#                            prediction rather than a rationalisation. On their
#                            request 879 it predicted which fields would fail,
#                            and that is where the round landed.
FRAME = """\
You are opening this turn. You are NOT answering it and NOT fetching anything.

Evidence from the pre-round tools is already in front of you. Read it first.

Say, briefly:

  what_kind_of_question   what is being asked, in your own words — and WHAT
                          KIND OF DOCUMENT answers questions like it. The kind
                          of document, not the topic: it shapes everything after.
  already_answered        what the evidence in hand already settles. If it
                          settles the whole question, say so plainly. Do not
                          manufacture a gap to justify another round.
  still_open              what is genuinely missing, named specifically enough
                          that someone could go and look for it.
  tools_i_passed_over     tools you considered and did not choose, one clause
                          each on why not.
  what_i_expect_to_be_hard  which parts you expect to FAIL, and why — written
                          now, before anyone tries.
"""

# ── EXPLORE ─────────────────────────────────────────────────────────────────
# deep-research's plan fields, minus `stop_rule`. They were explicit that a
# stop rule is a property of the TURN here and not of the plan: their executor
# walks plans to exhaustion inside one round, ours has a governor deciding
# whether there IS another. Asking a round-scoped planner for a stop rule
# invites it to promise something it does not control. `would_establish`
# replaces it — the field their architecture does not need and ours does.
EXPLORE = """\
You are closing ONE named gap this round. You are not re-asking the question.

For each tool you name:

  ask                what to send it. THIS IS A QUERY, NOT A QUESTION. Measured
                     on our own corpus: a question naming the payer built a
                     324-document pool and reached zero billing codes; the same
                     question written as retrieval vocabulary built 1,814 and
                     returned three codes with their rates.
  holds_this_if      what would have to be TRUE for this tool to cover this
                     subject. Do not plan a tool because it exists.
  cheap_test         how to find that out cheaply, or null.

If you name more than one tool:

  independent_because  what makes this a DIFFERENT theory rather than a retry.
                       Apply the test before you write the second one down: if
                       the first returns nothing, does that tell you anything
                       about whether the second will work? If it does, they are
                       ONE plan with two steps — order them. If it does not,
                       they are independent — name them together and they run
                       in the same round.

And one clause, which the governor reads when deciding whether to buy another
round after this one:

  would_establish    what this round settles if it works. Not what it might
                     find — what it would let us stop asking.
"""

# ── NARROW ──────────────────────────────────────────────────────────────────
# deep-research cautioned that "not worth buying" rests on knowing what a gap
# COSTS, and that a posture deciding on a guessed price cannot justify itself.
# Ours rests on TIME, not cost, and posture.spendable() records why: cost is
# reported on 686 of 1,236 react_1 calls, so spend is understated exactly where
# the governor steers, and an understated spend FAILS OPEN. Time decides
# because the promise clock is complete. That is stated in the prompt so the
# model does not offer a cost argument the system cannot support.
NARROW = """\
The budget will not fund another search. Do not attempt one, and do not
propose one as a next step — the decision was time, and the clock is complete.

State:

  answered        what the evidence settles, with its citations.
  left_open       each gap that remains, named precisely. A gap named exactly
                  is more useful than a padded attempt to close it.
  what_would_close_it   for each — what would have to exist or be true. If the
                  answer is "a source we do not have", say which source.
"""

# ── ALTERNATIVES ────────────────────────────────────────────────────────────
# deep-research's `only_one_route` and `afterword.what_is_missing`, including
# the parenthetical they called load-bearing: "A FINDING, NOT A COMPLAINT".
# Without it the model grumbles; with it the output accumulates into a queue of
# things worth building.
ALTERNATIVES = """\
This gap cannot be closed from our sources. Saying only that is honest and
useless. Give the person a route.

  who_holds_it    who actually has this — a payer's provider relations, a state
                  agency, a published schedule. Be specific.
  how_to_ask      what to ask them for, in their vocabulary.
  why_we_could_not  use what was already tried. "Three corpus searches returned
                  payer manuals and no fee schedule" says our corpus does not
                  carry this, which points somewhere. "Not found" does not.
  what_is_missing a source or capability that does not exist and would have
                  made this answerable. A FINDING, NOT A COMPLAINT — name the
                  thing that would have to be built.
"""

# ── VALIDATE ────────────────────────────────────────────────────────────────
# 🔴 THE KNOWN LIMIT, STATED HERE SO IT IS NOT DISCOVERED LATER. deep-research
# distinguishes a source that IS the rule from one that REPEATS it
# (`governing` vs `reproduces`), decided by a verifier that opens the cited
# source. We have authority per DOCUMENT (contract_source_of_truth /
# payer_policy / ...), not per claim-against-source. So this posture asks the
# model for a judgement we cannot independently check, and the prompt says so
# rather than implying a check exists.
VALIDATE = """\
Do not add anything. You are checking what is already written against the
evidence cited for it.

For each claim:

  carried_by      does the cited source actually SAY this, or does it repeat
                  something said elsewhere? A payer's manual restating a state
                  rule is not the rule.
  unsupported     any claim the cited evidence does not carry.

You may REMOVE a claim or soften it to what the evidence supports. You may not
extend, and you may not fetch.
"""

# ── COMMUNICATE ─────────────────────────────────────────────────────────────
# 🔴 HELD. This posture produces what a person reads, and the Deterministic UX
# seat owns how it renders. Four questions are with them: where the line falls
# between content and formatting; which fields the deterministic path needs
# present; whether the two branches (a complete answer, versus an honest
# failure) want one prompt or two; and what of v1's `draft` role to drop.
#
# The failure branch became load-bearing on 2026-09-15: v2's fallback to v1 was
# removed, so a failed turn now publishes ITS OWN honest failure rather than v1
# quietly answering instead.
COMMUNICATE = None

POSTURE_PROMPTS: dict[Posture, str | None] = {
    Posture.FRAME: FRAME,
    Posture.EXPLORE: EXPLORE,
    Posture.NARROW: NARROW,
    Posture.ALTERNATIVES: ALTERNATIVES,
    Posture.VALIDATE: VALIDATE,
    Posture.COMMUNICATE: COMMUNICATE,
}
