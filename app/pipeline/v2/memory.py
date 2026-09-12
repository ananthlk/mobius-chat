"""THE MEMORY MANAGER: one owner for what a turn remembers, and for how long.

Ananth, 2026-09-12: "YOU NEED A MEMORY MANAGER MODULE TO DO THIS UNLESS THERE
IS ALREADY ONE THAT WORKS".

There is not one. There are THREE partial mechanisms that do not know about
each other, and adding a fourth is what this module exists to prevent:

  ctx._evidence_memory       ONE TURN, in-process. Chunks with "call.chunk"
                             refs; read by the recall_evidence tool and by
                             stages/integrate.py. Dies with the turn.
  app/persistence/memory.py  WORKER PROCESS, 30-minute TTL. Session state for
                             when the DB is absent. Unrelated to evidence.
  thread_evidence (071)      THREAD, durable. Facts and verdicts. Written and
                             read through v2/store.py.

This module owns the POLICY across those tiers -- what is remembered, at which
lifetime, and what gets carried into the next prompt. v2/store.py stays as the
I/O layer beneath it and does no deciding.

THE RULE THAT MAKES THE TIERS WORTH SEPARATING: each tier answers a different
question, and collapsing them loses the answer.

    turn     "what did we just read?"          chunks, verbatim, expensive
    thread   "what do we already know?"        facts, ~100 chars, cheap
    absent   "we have never looked at this"    the absence of a row

That third one is a tier, not a gap. A row saying "unknown" would make
could-not-check indistinguishable from checked-false, which is the collapse
every other part of this module exists to remove.

WHAT IS DELIBERATELY NOT HERE: no summarisation, no embedding, no ranking of
facts. A memory manager that decides which facts MATTER is a second author of
the answer; this one decides which facts SURVIVE, and the order they arrived in
is the only order it uses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# How many stored facts ride into a prompt. Facts are cheap (~100 chars each
# against ~9,000 for the passage behind one) but they are not free, and an
# unbounded list would grow until it was the context problem it was built to
# solve. 12 is a starting number and is meant to be tuned against the
# disagreement data, not defended.
CARRY_FACTS = 12
# Known-useless sources are cheaper still -- a name and a page -- but the same
# argument applies one order of magnitude down.
CARRY_NOT_USEFUL = 8


@dataclass(frozen=True)
class Recall:
    """What this turn gets to start from."""
    facts: tuple[str, ...] = ()          # "<fact> [doc p12]", ready for a prompt
    not_useful: tuple[str, ...] = ()     # sources not to retrieve again
    turn_chunks: int = 0                 # how many chunks the turn tier holds
    # Why a tier is empty, when it is. "the store was unreachable" and "nothing
    # has been learned yet" produce the same empty list and carry opposite
    # advice, so the difference is recorded rather than inferred.
    unavailable: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_cold(self) -> bool:
        """Nothing known and nothing broken -- a genuinely new thread."""
        return not self.facts and not self.not_useful and not self.unavailable


def recall(ctx) -> Recall:
    """Everything the next prompt may start from, across tiers.

    Fail-soft per tier: a thread store that is down must not cost the turn its
    in-process memory, and vice versa. Each failure is NAMED in `unavailable`
    rather than silently producing an empty tier -- an empty memory and an
    unreachable one are different facts.
    """
    thread_id = str(getattr(ctx, "thread_id", "") or "")
    facts: list[str] = []
    not_useful: list[str] = []
    unavailable: list[str] = []

    # ── THREAD TIER: facts, durable, cheap to carry ──────────────────────
    if thread_id:
        try:
            from app.pipeline.v2 import store as _store
            useful_rows, nu = _store.load_evidence(thread_id)
            for row in useful_rows:
                label = str(row.get("label") or "")
                fact = str(row.get("fact") or "")
                # The fact is what we re-send; the label keeps it checkable.
                # A row with a label and no fact is still worth carrying -- it
                # says "this source was useful" even if the statement was not
                # captured -- so it degrades rather than disappearing.
                line = f"{fact} [{label}]" if fact else label
                if line and line not in facts:
                    facts.append(line)
            for label in nu:
                if label and label not in not_useful:
                    not_useful.append(label)
        except Exception as e:
            unavailable.append(f"thread store unreachable ({type(e).__name__})")
            logger.warning("[v2.memory] thread tier unavailable thread=%s: %s",
                           thread_id[:12], e)
    else:
        unavailable.append("no thread_id — nothing can be remembered for later")

    # ── TURN TIER: chunks, in-process, already in the prompt ─────────────
    # COUNTED, NOT COPIED. These chunks are already reachable this turn via
    # recall_evidence and integrate; re-rendering them into the prompt would
    # send the same evidence twice, which is the cost this whole exercise is
    # trying to remove.
    turn_chunks = len(getattr(ctx, "_evidence_memory", None) or [])

    return Recall(facts=tuple(facts[:CARRY_FACTS]),
                  not_useful=tuple(not_useful[:CARRY_NOT_USEFUL]),
                  turn_chunks=turn_chunks,
                  unavailable=tuple(unavailable))


def remember(ctx, *, facts=(), not_useful=()) -> int:
    """Commit this round's verdicts to the thread tier. Returns rows written.

    ONLY GROUNDED FACTS ARE STORED. A fact with no document cannot be checked
    later and would re-enter the next turn as an unsourced claim wearing the
    authority of memory -- which is strictly worse than forgetting it.
    """
    thread_id = str(getattr(ctx, "thread_id", "") or "")
    if not thread_id:
        return 0
    grounded, ungrounded = [], 0
    for f in facts or ():
        doc = getattr(f, "document", None) if not isinstance(f, dict) else f.get("document")
        if doc:
            grounded.append(f if isinstance(f, dict) else {
                "document": f.document, "page": f.page, "fact": f.fact})
        else:
            ungrounded += 1
    if ungrounded:
        # Said out loud: a fact react produced and we refused to store is a
        # decision, and a silent one reads later as react never having said it.
        logger.info("[v2.memory] dropped %d ungrounded fact(s) thread=%s",
                    ungrounded, thread_id[:12])
    if not grounded and not not_useful:
        return 0
    try:
        from app.pipeline.v2 import store as _store
        return _store.record_evidence(
            thread_id,
            correlation_id=str(getattr(ctx, "correlation_id", "") or ""),
            useful=grounded, not_useful=tuple(not_useful or ()))
    except Exception as e:
        logger.warning("[v2.memory] remember failed thread=%s: %s",
                       thread_id[:12], e)
        return 0
