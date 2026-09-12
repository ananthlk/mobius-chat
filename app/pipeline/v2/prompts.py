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
