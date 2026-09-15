"""Ask for the next steps BEFORE the answer is finished, not after.

Ananth, 2026-09-15: "the trick is to predict when we are near an answer and ask
the next steps and asks prompt right then .. there is always intelligence which
is better than just throwing things in the kitchen table".

THE PROBLEM THIS SOLVES. next_steps and follow-up questions are written by a
model call. Run it at finalisation and it is pure blocking tail — the answer is
already written, the person is already waiting, and we add a round trip to show
them two lists. That is the 6.8s Ananth cut on 2026-09-13, and restoring the
lists put part of it back.

THE SIGNAL ALREADY EXISTS. posture.converging(state) answers "is there EVIDENCE
the next round closes this" — few gaps open, the gap count NOT rising, and
something actually came back on the last attempt. It was computed every round
and read by exactly one caller, to decide whether a turn may overrun its budget.

So: the first round where the governor can see the end coming, we start the
next-steps call IN THE BACKGROUND, against the running answer. The loop keeps
going. By the time the answer is final the reply is usually already in hand,
and finalisation awaits a future instead of starting a call.

WHY THE RUNNING ANSWER IS GOOD ENOUGH. These two lists are about the QUESTION
and the GAPS, not about the final wording: "what does this person do next" and
"what would they ask next" do not change when a later round tightens a sentence
or adds a citation. What WOULD invalidate them is the gap set changing
materially, and that is exactly what `converging` has already ruled out — a
rising gap count is one of its three disqualifiers.

WHAT THIS IS NOT. Not speculative execution of a tool, and not a second
producer: it is the SAME integrator prompt, run earlier, whose result the same
consumer reads. If the prediction is wrong the future is simply discarded and
finalisation calls as it does today — a wasted call, never a wrong answer.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor

logger = logging.getLogger(__name__)

#: One slot per turn. A second prediction on a later round must not start a
#: second call — the lists would be near-identical and we would pay twice.
_ATTR = "_v2_ahead_next_steps"
_ATTR_POOL = "_v2_ahead_pool"


def predicted(state) -> bool:
    """Can the governor see the end from here?

    Delegates to posture.converging rather than inventing a second predicate:
    two answers to "are we nearly done" that can disagree is worse than none.
    """
    try:
        from app.pipeline.v2.posture import converging
        return bool(converging(state))
    except Exception:
        return False


def start(ctx, *, question: str, answer_so_far: str, open_gaps, runner) -> bool:
    """Begin the next-steps call now. Returns True if this call started it.

    Never raises and never blocks: a prefetch that can fail a turn is worse
    than no prefetch.
    """
    if getattr(ctx, _ATTR, None) is not None:
        return False
    if not (answer_so_far or "").strip():
        # Nothing to write next steps ABOUT yet. Firing here would ask the
        # model what follows an answer that does not exist.
        return False
    try:
        from app.pipeline.v2.integrator import (NEXT_STEPS_MAX_TOKENS,
                                                _next_steps_prompt)

        sys_p, user_p = _next_steps_prompt(question, answer_so_far, open_gaps)
        pool = ThreadPoolExecutor(max_workers=1,
                                  thread_name_prefix="v2-ahead")
        setattr(ctx, _ATTR_POOL, pool)
        fut = pool.submit(runner, sys_p, user_p,
                          max_tokens=NEXT_STEPS_MAX_TOKENS,
                          stage="v2_next_steps")
        setattr(ctx, _ATTR, fut)
        logger.info("[v2.ahead] cid=%s next_steps started early (answer=%d chars, "
                    "gaps=%d)", (getattr(ctx, "correlation_id", "") or "")[:8],
                    len(answer_so_far or ""), len(tuple(open_gaps or ())))
        return True
    except Exception:
        logger.warning("[v2.ahead] could not start early", exc_info=True)
        return False


def take(ctx) -> Future | None:
    """Hand the running call to whoever finalises, once."""
    fut = getattr(ctx, _ATTR, None)
    if fut is not None:
        try:
            setattr(ctx, _ATTR, None)
        except Exception:
            pass
    return fut


def shutdown(ctx) -> None:
    """Release the pool. Called on the way out, never on the hot path."""
    pool = getattr(ctx, _ATTR_POOL, None)
    if pool is not None:
        try:
            pool.shutdown(wait=False)
        except Exception:
            pass
        try:
            setattr(ctx, _ATTR_POOL, None)
        except Exception:
            pass
