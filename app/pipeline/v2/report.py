"""The standard round report: one shape, every round, machine- and human-readable.

Ananth, 2026-09-12:

    "can we plug in this our standard structure and start emitting this.. the
     summary is the role it assumed, the overall thread summary, turn summary,
     react round findings, open gaps, next round tools, expanded answer,
     is_complete.. is there anything else"

    "good news i am also working to get an enriched output not required because
     i can format it"

THAT LAST LINE IS THE POINT. This structure replaces the enricher, so it must
carry everything a formatter needs and nothing it has to go looking for. A
field that a renderer must fetch from somewhere else is a field this structure
failed to carry.

FOUR FIELDS BEYOND THE EIGHT ASKED FOR, each because its absence cost something
today:

  evidence     doc+page for what this round used. The expanded answer carries
               citation markers; without provenance in the SAME structure those
               markers have no referent -- which is how [1,2,3,4,5] ended up
               pointing at evidence that was never in the prompt.
  set_aside    what was read and rejected. Already computed, never shown, and
               it is what stops the next round re-retrieving it.
  budget       elapsed against the promise, and rounds left. The objective is
               quality SUBJECT TO the promise; a structure that never states
               the constraint cannot show it binding. Measured: round 2 costs
               3.26x round 1, so "is there a next round" is the most expensive
               decision described here.
  complete_why one clause. is_complete alone cannot be disagreed with; the
               reason is what makes "complete with gaps still open" visible.

EMPTY IS NOT THE SAME AS ABSENT. Every field distinguishes "we know this and it
is empty" from "we do not know this", because collapsing those is the defect
this whole module exists to remove -- never-searched vs searched-and-empty, one
level up.

PURE. Facts in, (text, dict) out. No ctx, no I/O.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# The expanded answer is the deliverable, not a progress note, and it is only
# rendered when the round's job is to COMMUNICATE. Emitting it every round
# would publish a half-written answer as if it were final -- and on a turn that
# takes three rounds, publish it three times.
EXPANDED_ANSWER_ROLE = "role_communicate"


@dataclass(frozen=True)
class RoundReport:
    round_index: int = 0
    roles: tuple[str, ...] = ()          # what this round assumed it was doing
    thread_summary: str = ""             # across the whole conversation
    turn_summary: str = ""               # this question, so far
    findings: tuple[str, ...] = ()       # what THIS round learned
    open_gaps: tuple[str, ...] = ()
    next_tools: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()       # doc p12 — what the answer rests on
    set_aside: tuple[str, ...] = ()      # read and rejected; do not re-fetch
    expanded_answer: str = ""            # only on a COMMUNICATE round
    is_complete: bool | None = None      # None = react has not said yet
    complete_why: str = ""
    # ASKED, NOT OBEYED. Ananth: "do you think another round is worth it to
    # improve the score.. we dont have to rely on it, just asking may be
    # helpful". Recorded so the DISAGREEMENT rate can be measured later --
    # how often "worth it" was followed by a round that closed nothing.
    # Nothing reads this to decide anything today, and that is deliberate.
    next_round_worth_it: bool | None = None
    next_round_why: str = ""
    # Budget: the constraint the whole objective function is subject to.
    elapsed_s: float | None = None
    promise_s: float | None = None
    rounds_left: int | None = None
    # Anything the round could not determine, named rather than omitted.
    unobservable: tuple[str, ...] = field(default_factory=tuple)


def to_dict(r: RoundReport) -> dict:
    """The machine-readable form, for the envelope and for a formatter.

    Keys are ALWAYS present. A formatter that has to test for a key's existence
    ends up inventing a default, and that default becomes a second author of
    the answer.
    """
    return asdict(r)


def _bullets(items, indent="     ") -> list[str]:
    return [f"{indent}- {i}" for i in items]


def render(r: RoundReport) -> str:
    """The human-readable form, for the thinking stream."""
    out: list[str] = []
    head = f"  ┌ round {r.round_index}"
    if r.roles:
        head += " · " + " · ".join(x.replace("role_", "") for x in r.roles)
    out.append(head)

    if r.thread_summary:
        out.append(f"  │ thread:   {r.thread_summary[:160]}")
    # "nothing yet" is said out loud: a missing line reads as a round that
    # produced nothing, which is a different claim from a round that has not
    # produced an answer YET.
    out.append(f"  │ turn:     {r.turn_summary[:220] if r.turn_summary else 'nothing yet'}")

    if r.findings:
        out.append("  │ found:")
        out.extend(f"  │{b}" for b in _bullets(r.findings))
    if r.evidence:
        out.append(f"  │ evidence: {'; '.join(r.evidence[:4])}")
    if r.set_aside:
        out.append(f"  │ ✗ aside:  {'; '.join(r.set_aside[:3])}")

    out.append("  │ open:     "
               + ("; ".join(r.open_gaps[:4]) if r.open_gaps else "none"))
    # An absent tools line reads as "no tools needed"; it usually means the
    # selector never answered.
    out.append("  │ next:     "
               + (" · ".join(r.next_tools) if r.next_tools
                  else "(none offered — selector unavailable)"))

    if r.elapsed_s is not None and r.promise_s is not None:
        over = " OVER" if r.elapsed_s > r.promise_s else ""
        left = "" if r.rounds_left is None else f", {r.rounds_left} round(s) left"
        out.append(f"  │ budget:   {r.elapsed_s:.1f}s of {r.promise_s:.0f}s{over}{left}")

    if r.unobservable:
        # COULD NOT CHECK is not CHECKED FALSE. Eight instances of that
        # collapse this session; it gets its own line so it cannot be read as
        # an empty result.
        out.append(f"  │ ? unknown: {'; '.join(r.unobservable[:3])}")

    if r.next_round_worth_it is not None:
        why = f" — {r.next_round_why}" if r.next_round_why else ""
        out.append(f"  │ another?  {str(r.next_round_worth_it).lower()}{why}"
                   "   (asked, not obeyed)")
    if r.is_complete is None:
        out.append("  │ complete: not stated")
    else:
        why = f" — {r.complete_why}" if r.complete_why else ""
        out.append(f"  │ complete: {str(r.is_complete).lower()}{why}")

    if r.expanded_answer and EXPANDED_ANSWER_ROLE in r.roles:
        out.append("  │ answer:")
        out.extend("  │     " + ln for ln in r.expanded_answer.splitlines())
    out.append("  └")
    return "\n".join(out)
