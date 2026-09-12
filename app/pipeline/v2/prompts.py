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

  "facts": [{"fact": "<one thing you now know, in one sentence>",
             "document": "<the document it came from>",
             "page": <page number>}]
      Only what THIS round's evidence supports, one sentence each. A fact with
      no document is DROPPED — it cannot be checked later, so it must not be
      remembered as if it could.

  "not_useful": ["<document, or document p<page>, that you read and are NOT using>"]
      What you looked at and rejected. Recorded so no later round retrieves or
      re-reads it.

Omitting facts[] means this turn learns NOTHING: the evidence dies with the
round and the next round starts blind."""


# ── DOMAIN CONTEXT ─────────────────────────────────────────────────────────
#
# Ananth, 2026-09-12: "not sending the program context makes react less of a
# healthcare analyst .. case management and care management are
# interchangeable".
#
# He is right and it showed in the answers: react treated "care management" and
# "case management" as different things and reported a gap for Sunshine Health
# while holding passages about its case management programme.
#
# 🔴 INTERIM, AND SOURCED WHERE IT CAN BE. Terminology is the Lexicon seat's —
# a list maintained here is a second vocabulary that drifts from theirs the
# first time either changes. Payer aliases already come from
# config/payer_normalization.yaml (their file, not mine). The equivalences
# below are the minimum needed for the questions we are testing, and they are
# marked as interim rather than presented as a vocabulary.
DOMAIN_CONTEXT = """

── DOMAIN CONTEXT (Florida Medicaid managed care) ──
You are reading payer policy documents as a healthcare policy analyst.

TERMS THAT MEAN THE SAME THING in these manuals — treat a passage using one as
evidence for a question asking the other:
  • care management ≡ case management ≡ care coordination ≡ care management
    program / model
  • member ≡ enrollee ≡ beneficiary
  • provider manual ≡ provider handbook ≡ provider reference guide
  • prior authorization ≡ PA ≡ pre-service review ≡ prior approval
  • timely filing ≡ claim submission deadline ≡ filing limit

A payer describing its "case management program" IS describing its care
management approach. Do not report a gap for a term when the evidence uses its
equivalent."""


def system_suffix(ctx) -> str:
    """v2's additions to the reasoning system prompt. Empty for v1.

    Returns "" rather than v1's text for a non-v2 arm: a v2 module must never
    hand v1 a value it chose.
    """
    if str(getattr(ctx, "orchestrator_version", "v1")) != "v2":
        return ""
    return DOMAIN_CONTEXT + RESPONSE_SHAPE_SUFFIX
