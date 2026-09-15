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
# From the Deterministic UX seat, docs/COMMUNICATE_PROMPT_UX_INPUT.md (be4772a).
# Every number below is measured on 32 real A/B drafts, not asserted.
#
# THE LINE: the model says WHAT each thing IS; the classifier decides HOW it
# renders. Since 26facd1 `format` is DISCARDED on every v2 section and
# re-decided from content, so anything a prompt says about shape is dead on
# arrival. Two measurements of why it must be: the model called a label/value
# list "bullets", and called a SIX-item set "stats" into a renderer that draws
# four tiles — silently losing two rows every time.
#
# THE CEILING IS THE WRITING, NOT A FIELD. 19 of 32 drafts (59%) contain no
# structural block at all; 9 of 32 (28%) are a single unbroken paragraph. The
# contract already says "Do NOT write paragraphs" and is not being followed,
# so restating it in new words would not help. The one demand that moves the
# number is one assertion per line, labelled.
#
# TWO PROMPTS, NOT ONE WITH A BRANCH — their finding and the reason is
# mechanical, not stylistic: the formatter treats a thin-evidence turn as a
# REFUSAL and will not fill it, because a cited bullet list beside an
# ungrounded answer is the most confident-looking thing on a screen. A failure
# shaped like a success fights the gate that keeps it honest.
COMMUNICATE = """\
You have what you are going to have. Write the answer.

MARK WHAT THINGS ARE. Do not decide how they look — that is chosen downstream
from what you write, and any shape you name is discarded.

  a label     lead a line with the thing it is about: "Initial claims: ..."
              Measured: on one question the labelled arm led 4 of 4 lines with
              a label and became a four-row table; the unlabelled arm led 0 of
              4 and could not, because there is nowhere to split subject from
              predicate without guessing.
  an order    if sequence carries meaning, write them as steps. Only you know
              whether it does.
  a peer set  things of the same kind go on sibling lines, never merged into
              one paragraph.

ONE ASSERTION PER LINE. Every distinct thing this answer claims gets its own
line, with a short label where one exists. This is the single change that
matters: 19 of 32 real answers contained nothing to structure at all.

KEEP A LINE SCANNABLE. Past roughly 25 words a line stops being readable at a
glance.

DO NOT OPEN WITH A GREETING. Not "Hey", not "I've got that for you", not "Here
is what I found". 22 of 32 real answers opened with one, and it is worse than
noise: the renderer keeps prose BETWEEN structural blocks, so on a
well-structured answer the greeting is the only prose that survives. Start
with the answer.
"""

# 🔴 THE FAILURE BRANCH IS ITS OWN PROMPT, AND IT ASKS FOR NO STRUCTURE.
#
# Load-bearing since v2's fallback to v1 was removed on 2026-09-15: a failed
# turn now publishes ITS OWN words rather than v1 quietly answering instead, so
# those words are the product.
#
# The four failures below are distinguished everywhere else in this system and
# collapse in the answer into "I could not find...", which is true of all four
# and useful for none.
COMMUNICATE_FAILURE = """\
This turn did not produce an answer. Say so plainly, in prose. Do not write
labels, lines or sections — a failure shaped like an answer reads as a
confident one.

SAY WHICH FAILURE IT WAS. These are not the same and the person can act on
only some of them:

  we did not look        a tool did not run. Nothing was searched, so this
                         says NOTHING about whether the information exists.
  we looked, not there   the source was searched and does not carry it.
  we could not check     we have something, but no way to confirm it.
  it did not hold up     we checked, and the evidence did not support it.

Then say what would resolve it — a source to consult, a person to ask, a
question asked differently. If nothing would, say that.

Do not apologise, do not describe the tools, and do not offer a summary of
what you were unable to do.
"""

POSTURE_PROMPTS: dict[Posture, str | None] = {
    Posture.FRAME: FRAME,
    Posture.EXPLORE: EXPLORE,
    Posture.NARROW: NARROW,
    Posture.ALTERNATIVES: ALTERNATIVES,
    Posture.VALIDATE: VALIDATE,
    Posture.COMMUNICATE: COMMUNICATE,
}
