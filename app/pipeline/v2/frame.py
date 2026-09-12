"""The governor's sections of the round frame, and the ack it asks for.

Spec: docs/governor-prompt-frame-v1.md

The governor owns THREE of the frame's ten sections, and they are exactly the
three things react cannot hold for itself:

    §5  OPEN GAPS   the cross-round ledger -- what is open, what was tried
                    against each, and what came back
    §6  ROLE        the role this round is for
    §8  COMPLETE?   the satisfaction question, and a second opinion

Everything else in the frame belongs to another seat (§1/§7 LLM, §2/§3 Chat,
§9 Tool Manifest, §4/§10 react's own) and is NOT rendered here. A section
rendered by two authors is a contradiction waiting for whoever debugs it next
-- the LLM seat made that argument about `closure` and it was right.

THE ACK IS A COMPREHENSION CHECK, NOT A COMPLIANCE CHECK. It proves the
section was read; the checker in compliance.py proves the round acted on it.
Both are recorded, and the DISAGREEMENT is the signal. An ack that has never
once contradicted the trace is measuring nothing and should be deleted.

PURE. State in, text out.
"""

from __future__ import annotations

from app.pipeline.v2 import statements as ST
from app.pipeline.v2.posture import (
    Gap, Posture, latest_closure, targeted_attempts,
)

# Posture -> the role, in react's language rather than the machine's. NOT a
# posture name: "narrow" means nothing to the model, and a label it cannot act
# on is a label that changes nothing.
ROLE: dict[Posture, str] = {
    Posture.FRAME: "read the question and decide what it is actually asking",
    Posture.EXPLORE: "find evidence that closes a named open part",
    Posture.NARROW: "state what is claimed and what is still open — do not search",
    Posture.ALTERNATIVES: "offer a route to what could not be reached — do not search",
    Posture.VALIDATE: "check what is already claimed against the evidence kept",
    Posture.COMMUNICATE: "write the answer from what is kept — do not search",
}

# Only the acks whose CHECKER exists. An ack with no checker is a producer with
# no consumer wearing a schema, and it reads as evidence while proving nothing.
ACK_KEYS: tuple[tuple[str, str], ...] = (
    ("parts", "the distinct parts of the question you must answer"),
    ("working_gap", "the id of the ONE open part you are working this round, "
                    "or null if you are not searching"),
    ("complete", "true/false — your own call"),
    ("complete_why", "one clause: why"),
    ("dissent", "if a second opinion is given above: 'accepted' or "
                "'declined — <reason>'"),
)


def _gap_line(g: Gap, current_round: int) -> list[str]:
    tried = targeted_attempts(g)
    out = [f"  [{g.gap_id}] {g.text}"]
    if not tried:
        # The distinction the whole ledger exists for. "Never searched" and
        # "searched and empty" produce the same sentence in an answer today.
        out.append("      attempts: none — this part has never been searched")
    for a in tried:
        got = "returned evidence" if a.returned_payload else "returned nothing"
        out.append(f"      r{a.round_index} {a.tool} {a.query!r} -> {got}")
    c = latest_closure(g)
    if c is not None and c.value is not None:
        prior = [x for x in g.closure_by[:-1]
                 if x.supported and x.value is not None]
        was = f" (was {prior[-1].value})" if prior else ""
        note = "" if c.supported else "  [not corroborated by evidence]"
        out.append(f"      closure: {c.value}{was}{note}"
                   + (f" — {c.why}" if c.why else ""))
    return out


def render(c: ST.Ctx, posture: Posture) -> tuple[str | None, ST.Selection]:
    """The governor's sections, in execution order, plus the ack request."""
    sel = ST.select(c, posture)
    gaps = c.state.open_gaps
    parts: list[str] = []

    # ── §5 OPEN GAPS ────────────────────────────────────────────────────────
    if gaps and c.round_index > 1:
        parts.append("[§5 OPEN PARTS — the governor's ledger across rounds]")
        for g in gaps:
            parts.extend(_gap_line(g, c.round_index))

    # ── §6 ROLE ─────────────────────────────────────────────────────────────
    role = ROLE.get(posture)
    if role:
        parts.append(f"[§6 ROLE this round] {role}")

    # ── §8 COMPLETE? and the rest of the steering ───────────────────────────
    settle = [s for s in sel.statements if s.slot == ST.Slot.SETTLE]
    other = [s for s in sel.statements if s.slot != ST.Slot.SETTLE]
    if settle:
        parts.append("[§8 IS THIS COMPLETE?]")
        parts.extend(f"  - {ST.text_of(s, c)[0]}" for s in settle)
    if other:
        parts.append("[Governor — this round]")
        parts.extend(f"  - {ST.text_of(s, c)[0]}" for s in other)

    if not parts:
        return None, sel

    # ── the ack ─────────────────────────────────────────────────────────────
    parts.append("[ACK — return these in your JSON as \"ack\": {...}]")
    parts.extend(f"  {k}: {desc}" for k, desc in ACK_KEYS)
    parts.append("  Each is checked against what this round actually does. "
                 "A claim here that the round contradicts is worse than "
                 "leaving it out — say what you are doing, not what looks right.")

    return "\n".join(parts), sel
