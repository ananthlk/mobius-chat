"""The retrieval ladder, READ from Deep Research rather than paraphrased.

🔴 WHY THIS IS A FETCH AND NOT A STRING.

The rung wording is theirs and it is measured. Copying it here would make a
second copy of a vocabulary that already exists in their release, and the
first time they learn something the copy would be wrong and nobody would
notice. Same seam as `size_class`, one level up.

WHAT WE TAKE AND WHAT WE DO NOT. Deep Research, 2026-09-19, after a 7x change
in their base:

    "Use the ladder's ORDERING, which is mechanical and has held through a 7x
     change in base; do not use its magnitudes."

So this module reads the RUNGS and never the odds. Their pooled read_whole
figure moved 95% -> 45% when the base went from 19 rulings on one document to
522 over twelve, and within their own domain it ranges 24%-77% by service
line. Aetna payor documents are a different corpus again. A number that
unstable is not something to put in a prompt; the ordering is.

AND THE NUMBER WE ALMOST BUILT ON WAS NOT MEASURING WHAT IT SAID. Their
`retrieve / reformulate` at 19% was the ELSE branch of a prose classifier —
607 of 607 rulings fell through to it, none positively identified. It read
"reformulation settles 19%" and meant "everything we could not classify
settles 19%". I was one edit from declining to build reformulation on it.

FAILS OPEN, SHORT LEASH. This sits on the prompt path: explicit timeout,
process-lifetime cache, empty string on any failure. A round without the rung
is the round we shipped yesterday; a round behind a hanging socket is a turn
nobody gets.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

GUIDANCE_URL = os.environ.get(
    "MOBIUS_RESEARCH_GUIDANCE_URL",
    "https://mobius-deep-research-1032922478554.us-central1.run.app"
    "/api/skills/v1/research/guidance/retrieval")

TIMEOUT_S = 3
_CACHE: str | None = None


def _rungs() -> list[dict]:
    req = urllib.request.Request(GUIDANCE_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        body = json.loads(r.read())
    return [x for x in (body.get("ladder") or []) if isinstance(x, dict)]


def name_a_document_block() -> str:
    """The rung to take when what you hold does not answer the question.

    Rendered only if the rung is REACHABLE from here (`as_tool`), because a
    rung naming a capability this loop cannot invoke teaches the model to plan
    a move nobody can execute.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        rungs = {r.get("name"): r for r in _rungs()}
    except Exception as exc:
        logger.warning("[v2.ladder] guidance unreadable (%r) — the round ships "
                       "without the rung", exc)
        _CACHE = ""
        return _CACHE

    parts: list[str] = []
    for name in ("reformulate_to_name", "read_whole"):
        r = rungs.get(name) or {}
        if not r.get("as_tool"):
            continue
        move = " ".join(str(r.get("move") or "").split())
        cond = " ".join(str(r.get("condition") or "").split())
        ask = " ".join(str(r.get("escalation_question") or "").split())
        tool = r.get("tool") or "?"
        parts.append(f"  {name.replace('_', ' ').upper()} — {cond}\n"
                     f"      Tool: {tool}\n"
                     f"      {move}\n"
                     f"      Ask yourself: {ask}")
    if not parts:
        _CACHE = ""
        return _CACHE

    _CACHE = (
        "\n\nWHAT YOU ARE HOLDING DOES NOT ANSWER THIS, AND YOU HAVE NOT YET\n"
        "ASKED THE SECOND QUESTION. Before you report that something was not\n"
        "found, take the next rung:\n\n"
        + "\n\n".join(parts)
        + "\n\n  REWORDING TO ANSWER RE-RUNS THE SAME CONTEST. Asking the same\n"
          "  ranker the same question in different words puts you back in the\n"
          "  contest you just lost. Naming a document changes the KIND of\n"
          "  question and unlocks reading it whole.\n\n"
          "  If you genuinely cannot name a document, say so plainly — that is\n"
          "  a different and more useful statement than \"not found\"."
    )
    return _CACHE
