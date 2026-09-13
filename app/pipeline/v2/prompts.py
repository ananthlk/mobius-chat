"""V2'S OWN PROMPT SURFACE. Nothing about v2 belongs in react/prompts.py.

Ananth, 2026-09-12: "make sure v2 does not contaminate v1 in anyways.. v1
should stay pure for a/b compares.. including prompts and everything", then
"maybe you should replicate your prompts to v2 something so that there is no
duplication and create a prompts v2 so that it is clean too".

WHY THIS FILE EXISTS. The A/B fork only measures ONE change if v1 behaves
exactly as it did before v2 existed. I had already broken that once: I raised
the react output ceiling 1400 -> 8000 inside _call_llm_json, which serves BOTH
arms, so v1's truncation behaviour moved underneath the comparison and every
number after it would have been measuring two changes at once — while looking
like one.

NOT A COPY OF react/prompts.py. Duplicating the response_shape would create two
texts that drift, and the drift would show up as an A/B difference nobody
introduced deliberately. This file holds only what is v2's OWN:

  * v2's call parameters (its output ceiling), passed EXPLICITLY so the shared
    floor never has to know an arm exists. A floor never lowers a caller's ask,
    which is why this works without touching v1 at all.
  * v2's own prompt text lives in v2/frame.py (§5/§6/§8/§9/§10/§11),
    v2/blocks.py (the role stack) and v2/integrator.py (critic, next steps) —
    all rendered ONLY into the governor block, which only v2 receives.

THE RULE FOR ANYONE EDITING EITHER SIDE: if a change is for v2, it lands here
or in another v2/ module. If it lands in react/prompts.py, react_loop.py's
shared path, or the shared response_shape, it changes v1 and invalidates the
comparison. tests/test_v1_purity.py enforces this.
"""

from __future__ import annotations

# v2's reasoning-round output ceiling.
#
# MEASURED on gemini-2.5-flash: the ceiling INCLUDES THINKING TOKENS, so a
# budget sized to the visible output truncates. With the v2 critic prompt,
# whose visible reply is ~900 characters:
#     900   ->  166 chars, cut mid-word
#     3000  ->  936 chars, cut mid-word
#     8000  ->  complete, parses
#
# v2 rounds also return facts[] with provenance ON TOP OF evidence_review,
# which is strictly more output than v1's 1400 was set for. A truncated round
# surfaces downstream as "Could not parse model decision" — a diagnosis that
# sends the reader to a prompt that was already correct.
#
# THE CEILING IS NOT A COST: we pay for tokens PRODUCED, not offered.
# llm_calls.output_tokens records what was used; trim against that, not a guess.
# Ananth: "lets start being generous and we can pair it down based on what it
# uses".
ROUND_MAX_TOKENS = 8000


def round_max_tokens(ctx) -> int | None:
    """v2's output ceiling for a reasoning round, or None to leave v1 alone.

    Returns None for v1 EXPLICITLY rather than returning v1's number, so a
    caller cannot accidentally hand v1 a value this module chose. v1's ceiling
    is v1's business and lives in react/prompts.py where it was measured.
    """
    if str(getattr(ctx, "orchestrator_version", "v1")) != "v2":
        return None
    return ROUND_MAX_TOKENS


# ── v2's RESPONSE SHAPE, in the SYSTEM prompt where shapes are obeyed ───────
#
# MEASURED: stating this in the governor block (a USER-message addendum) was
# not enough. Across runs of one question with identical code, react returned
# facts[] sometimes and v1's shape other times — and strengthening the wording
# from "ALSO RETURN these two keys" to a complete superseding object did NOT
# fix it. The authoritative response shape lives in the 51,670-character SYSTEM
# prompt; an addendum in the user message does not outrank it.
#
# So v2 appends to the SYSTEM prompt. react/prompts.py is untouched and stays
# pristine for v1 — Ananth: "why llm seat.. because they own us the right
# output??" No: that file is shared, and putting facts[] in it would start
# asking v1 for facts too. v2's shape is v2's, and this is where it goes.
RESPONSE_SHAPE_SUFFIX = """

── ADDITIONAL REQUIRED KEYS (orchestrator v2) ──
Your JSON response must ALSO contain these two keys. Everything described
above still applies; these are added, nothing is replaced.

  "facts": [{"fact": "<one thing you now know, in the source's own words>",
             "document": "<the document it came from, EXACTLY as written above>",
             "page": <page number>}]
      Only what THIS round's evidence supports, one sentence each.

      KEEP THE SOURCE'S OWN WORDING. Stay as close to the manual's exact
      phrasing as you can — trim, do not rewrite. "The ICM program is based on
      a Member advocacy philosophy, designed and administered to assure the
      Member value-added coordination" is a good fact. "Molina takes a
      member-first approach to care" says the same thing and is a bad one:
      nobody can find it on the page.

      These facts are checked against the cited page afterwards, by matching
      them to the text. The closer your wording is to the source, the more
      certainly a true claim is confirmed — and a paraphrase of something real
      can fail that check and be dropped as unsupported. Paraphrasing costs you
      nothing to write and costs the reader a fact.

      Copy the document name EXACTLY as it appears in the evidence above. A
      shortened or tidied filename cannot be found, and the claim is then
      unverifiable however true it is.

      A fact with no document is DROPPED — it cannot be checked later, so it
      must not be remembered as if it could.

  "not_useful": ["<document, or document p<page>, that you read and are NOT using>"]
      What you looked at and rejected. Recorded so no later round retrieves or
      re-reads it.

Omitting facts[] means this turn learns NOTHING: the evidence dies with the
round and the next round starts blind."""


# ── WHO YOU ARE WORKING FOR ────────────────────────────────────────────────
#
# Ananth, 2026-09-12, correcting my first attempt: "that is too specific.. i am
# saying react needs to know it is part of mobius and what we do and i think
# that will solve a lot of the gaps".
#
# My first version was a synonym table (care management ≡ case management, …).
# Too narrow twice over: it only covers terms I happened to notice, and
# terminology belongs to the Lexicon seat anyway. The general fix is that an
# analyst who knows the domain does not need the table — they read for the
# CONCEPT, not the label, because they know what the reader is going to do with
# the answer.
#
# GROUNDED, NOT INVENTED. The platform name and module map come from
# docs/platform-definition.json ("Mobius RCM Network", 44 modules). The domains
# below are the ones this codebase actually implements tools for — appeals
# (CARC/denial playbooks), credentialing/roster, prior auth, timely filing,
# care management, claims — not a market description I wrote. Anything I could
# not verify from the repo is absent rather than plausible.
PRODUCT_CONTEXT = """

── WHO YOU ARE WORKING FOR ──
You are part of MOBIUS RCM NETWORK. Mobius helps healthcare provider
organisations work with payer policy: appeals and denials, prior
authorisation, credentialing, claims and timely filing, care management.

WHO IS ASKING. The people using this are operators inside provider
organisations — revenue cycle staff, credentialing teams, clinical operations,
compliance. They are not asking out of curiosity. They are about to do
something: submit a claim, file an appeal, enrol a provider, design a care
programme, or decide whether a payer's rule applies to them.

WHAT YOU ARE READING. Payer provider manuals, policies, fee schedules and
contracts — mostly Florida Medicaid managed care. These are operational
documents written by payers for providers.

WHAT THIS MEANS FOR HOW YOU READ THEM
  • Read for the CONCEPT, not the label. These manuals describe the same
    operational thing under different names, and a payer describing its "case
    management program" is describing its care management approach. If the
    evidence answers the question under a different word, it answers the
    question — do not report a gap for the label.
  • An operator needs what the manual SAYS and what it means for them. A
    citation they can check beats a fluent summary they cannot.
  • Payers differ, and the differences are the point. When several are asked
    about, what each one does is more useful than what they have in common.
  • If something is genuinely not in the corpus, say so plainly and say which
    payer and which topic — that is an actionable answer in this domain, not a
    failure."""


def system_suffix(ctx) -> str:
    """v2's additions to the reasoning system prompt. Empty for v1.

    Returns "" rather than v1's text for a non-v2 arm: a v2 module must never
    hand v1 a value it chose.
    """
    if str(getattr(ctx, "orchestrator_version", "v1")) != "v2":
        return ""
    return PRODUCT_CONTEXT + RESPONSE_SHAPE_SUFFIX
