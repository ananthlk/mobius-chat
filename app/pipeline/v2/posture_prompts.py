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

from app.pipeline.v2 import plan_shape as _plan
from app.pipeline.v2 import size_class as _size_class
from app.pipeline.v2.posture import Posture

# ── FRAME ───────────────────────────────────────────────────────────────────
# deep-research's `survey` object, which sits ABOVE the plans and is emitted
# before any plan exists. Their two highest-value fields are kept and the
# reason each earns its place is written down:
#
#   tools_i_passed_over      turns "it never occurred to me" and "I considered
#                            and rejected it" into different VISIBLE states.
# CITATION BURDEN (deep-research, 2026-09-15). `already_answered` carried no
# citation requirement while NARROW's `answered` did — the same claim under two
# burdens, and the WEAKER one sat on the posture that runs FIRST and can end
# the turn. The cheapest path to a finished turn was the one path that did not
# have to cite anything. Their verifier, which checks every quote
# character-for-character, caught in two days: an ellipsis-stitched quote
# presented as verbatim, a quote assembled from two sections of one rule, and
# an answer built from a sentence about a DIFFERENT service. All three fluent,
# all three wrong, none catchable by a field that reports what evidence
# "settles" without naming where.
#
#   what_i_expect_to_be_hard written BEFORE planning, so it is a falsifiable
#                            prediction rather than a rationalisation. On their
#                            request 879 it predicted which fields would fail,
#                            and that is where the round landed.

# ── SILENCE IS NOT ABSENCE ──────────────────────────────────────────────────
# Declared ONCE and rendered into every posture that reads pre-round evidence.
# A second copy of this text is the drift this module exists to prevent.
#
# `rag` is in preload's ALWAYS_PRELOAD, so EVERY v2 turn now opens holding
# retrieved passages. That is a real improvement — three rounds became one —
# and it removed a guard without removing what the guard was for.
#
# Deep Research named it: their engine REFUSES to call its extractor on empty
# evidence, precisely so "nothing was retrieved" can never be rendered as "the
# document does not say it". A preload guarantees evidence is never empty, so
# that guard's TRIGGER is gone while its failure is not. And the pull is
# strong: the page is not blank, so ruling on it feels like a finding.
#
# mobius_contracts.taxonomies.tool_outcome.WORLD_CLAIMS is empty, deliberately:
# no tool outcome licenses telling a person "not found". The contract says a
# caller wanting that licence "must make the `absent` judgement itself and be
# able to name the document it read." This loop has no such layer. So it may
# not make the claim, and the prompt has to say so — the model cannot infer a
# constraint from a constant it never sees.
SILENCE_IS_NOT_ABSENCE = """\
WHAT THE PRE-ROUND EVIDENCE IS, BEFORE YOU RULE ON IT. A broad sweep on the
question's own words ran automatically, before you said anything. It is NOT a
targeted read of a named document.

So its SILENCE tells you nothing. Passages that do not mention a thing are
equally what you get when the sweep used the wrong words, when the document
that holds the answer was too large to be swept, and when the thing truly does
not exist. You cannot tell those apart from where you sit — and neither can
the person reading you.

Therefore: NEVER write that something "is not specified", "is not mentioned",
"the documents do not say", or any other claim about the WORLD, on pre-round
evidence alone. That is a finding about a SEARCH, delivered as a finding about
a POLICY, and it is the most expensive mistake available to you here: the
person stops looking for something that is there.

If what you hold is silent on the question, that is a GAP. Name it and plan a
tool that would close it. Silence is a reason to look, never a reason to
answer.
"""

FRAME = """\
You are opening this turn. You are NOT answering it and NOT fetching anything.

Evidence from the pre-round tools is already in front of you. Read it first.

Say, briefly:

  what_kind_of_question   what is being asked, in your own words — and WHAT
                          KIND OF DOCUMENT answers questions like it. The kind
                          of document, not the topic.
  expected_size_class     which size class that KIND usually falls in, from the
                          list below, and that you have not checked. Size, not
                          kind, is what decides which move is available.
  already_answered        what the evidence in hand already settles — WITH THE
                          DOCUMENT AND SENTENCE FOR EACH. If it settles the
                          whole question, say so plainly. Do not manufacture a
                          gap to justify another round. If a settlement cannot
                          be quoted, it has not settled anything.
  still_open              what is genuinely missing, named specifically enough
                          that someone could go and look for it.
  tools_i_passed_over     tools you considered and did not choose, one clause
                          each on why not.
  what_i_expect_to_be_hard  which parts you expect to FAIL, and why — written
                          now, before anyone tries.

{silence}{size_classes}
""".format(silence=SILENCE_IS_NOT_ABSENCE,
                         size_classes=_size_class.expected_block())

# ── EXPLORE ─────────────────────────────────────────────────────────────────
# The plan fields are NOT written here. They are declared once in
# plan_shape.py and rendered in — Deep Research asked for one place they
# could adopt, and a prompt string is not one.
# deep-research's plan fields, minus `stop_rule`. They were explicit that a
# stop rule is a property of the TURN here and not of the plan: their executor
# walks plans to exhaustion inside one round, ours has a governor deciding
# whether there IS another. Asking a round-scoped planner for a stop rule
# invites it to promise something it does not control. `would_establish`
# replaces it — the field their architecture does not need and ours does.
EXPLORE = """\
FIRST, READ WHAT YOU ALREADY HAVE. The pre-round tools have run and their
evidence is in front of you.

  already_answered   what that evidence ALREADY settles, with the document and
                     sentence for each. If it settles the whole question, SAY
                     SO AND STOP — write the answer now rather than buying a
                     round to re-find what you are holding. Do not manufacture
                     a gap to justify another round. A settlement you cannot
                     quote has not settled anything.

{silence}{escalation}

Only if something is genuinely still missing, plan for it.

NAME EVERY TOOL YOU NEED, NOT ONE. Tools you name together RUN IN THE SAME
ROUND, in parallel. Two independent theories named together cost one round;
named one at a time they cost two, and the person waits through both. Put them
in "tools": [{{"tool": ..., "inputs": {{...}}}}, ...].

For each tool you name:

{per_plan}

If you name more than one tool:

{across_plans}

And one clause, which the governor reads when deciding whether to buy another
round after this one:

{per_round}
""".format(
    silence=SILENCE_IS_NOT_ABSENCE,
    escalation=_size_class.escalation_block(),
    per_plan=_plan.render(_plan.PER_PLAN),
    across_plans=_plan.render(_plan.ACROSS_PLANS),
    per_round=_plan.render(_plan.PER_ROUND),
)

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
