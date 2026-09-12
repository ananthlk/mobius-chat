"""The governor's steering block: one gap, its history, and what to do next.

WHY THIS MODULE EXISTS. The governor selected a gap every round and wrote it
to ``turn_rounds.gap_targeted``. Nothing read it. Every consumer was a governor
module or the A/B harness, so the round prompt was never told which gap to
close and the model picked a query unaided -- on a three-payer question it
picked one payer and the governor's central decision was inert. 417 decisions
in 24h, none of which changed what was asked.

Ananth, 2026-09-12: *"target the first and most important... try closing
this... you have tried gap-1, if this round did not close it, keep at it, if
closed pick the next gap... so you govern it."*

WHAT THIS MODULE MAY NOT DO, and each is load-bearing:

  * It does NOT write the query. The governor decides WHERE to spend, never
    WHAT to ask. A governor writing queries is a second author of the answer,
    and the A/B could no longer attribute a divergence to the decision.
  * It names ONE gap. A list restates the question, which is what the model
    already had when it covered one payer and stopped.
  * It carries the ATTEMPT HISTORY. That is why REFORMULATE was folded into
    CLOSE: the distinguishing fact is data the governor holds, not a name it
    asserts.
  * It says what REMAINS. Suppressing the other gaps invites a premature
    "complete" -- the model has to know the turn is not over.

PURE. Text in, text out, no clock and no DB, same bar as posture.py -- a block
that cannot be rebuilt from a stored row cannot be replayed when a result is
disputed.

Spec: docs/governor-react-closure-contract.md (Direction 1).
"""

from __future__ import annotations

from app.pipeline.v2.posture import (
    Band, Gap, Trend, closure_trend, latest_closure, targeted_attempts,
)

HEADER = "[Governor]"


def _status_line(gap: Gap) -> str:
    tried = targeted_attempts(gap)
    c = latest_closure(gap)
    if not tried and c is None:
        return "status: not yet attempted"
    bits = []
    if tried:
        bits.append(f"attempted {len(tried)} round(s)")
    if c is not None and c.value is not None:
        prior = [x for x in gap.closure_by[:-1]
                 if x.supported and x.value is not None]
        was = f" (was {prior[-1].value})" if prior else ""
        bits.append(f"closure {c.value}{was}")
        if not c.supported:
            # Said plainly rather than silently discounted: the model is
            # entitled to know its own report was not corroborated.
            bits.append("NOT corroborated by evidence this round")
    return "status: " + ", ".join(bits)


def _instruction(gap: Gap) -> str:
    """What to do about THIS gap, from what the ledger actually shows."""
    tried = targeted_attempts(gap)
    c = latest_closure(gap)
    trend = closure_trend(gap)

    if not tried:
        return ("instruction: this gap has not been searched yet. Search for "
                "it specifically -- not for the question as a whole.")

    if c is not None and c.band is Band.NONE and c.supported:
        # The honest CAPABILITY signal, and the model is better placed than
        # the governor to know whether a different angle exists.
        return ("instruction: a targeted attempt returned nothing for this "
                "gap. Try a materially different angle, or say plainly that "
                "it cannot be closed and why -- do not repeat the last query.")

    if trend is Trend.INCREASING:
        return ("instruction: closure rose but this gap is not closed. Stay "
                "on THIS gap and ask for the part still missing.")

    if trend is Trend.DECREASING:
        return ("instruction: closure fell -- the last attempt moved away "
                "from this gap. Return to what was working.")

    # FLAT with attempts behind it. Same lever, same result.
    prior = ", ".join(f'"{a.query}"' for a in tried if a.query) or "(not recorded)"
    return ("instruction: this gap is not moving. Previous queries: "
            f"{prior}. Change the approach -- a reworded version of the same "
            "query returns the same evidence.")


def governor_block(gap: Gap | None, *, remaining: tuple[Gap, ...] = (),
                   round_index: int = 0) -> str | None:
    """The block appended to the round context, or None when there is nothing
    to steer.

    None -- not an empty string and not a generic nudge -- when no gap is
    targeted. A block that appears every round saying nothing trains the model
    to skip it, and then it is not there on the round that matters.
    """
    if gap is None:
        return None

    lines = [
        HEADER,
        f'close this gap: "{gap.text}"   (id {gap.gap_id})',
        _status_line(gap),
        _instruction(gap),
        "do not: open a new topic, or answer the other gaps, until this one "
        "closes or you report it cannot be closed.",
    ]
    others = [g for g in remaining if g.gap_id != gap.gap_id]
    if others:
        lines.append("still open after this one: "
                     + "; ".join(g.text for g in others))
    # Asked for by id so a rewording can still be matched to this gap -- gap
    # ids are content-addressed, so drift mints a new id and resets the age
    # and lever counts that make `stuck` reachable at all.
    lines.append(f'report progress for this gap in evidence_review.gaps as '
                 f'{{"id": "{gap.gap_id}", "closure": 0-100, "why": "..."}}')
    return "\n".join(lines)
