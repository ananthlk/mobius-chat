"""Periodic product-feedback cadence signal for a ReAct turn.

Extracted from react_loop.py so that file stays under its LOC ratchet
(tests/test_react_split_phase_1i). See docs/feedback-agent-spec.md §4B/§6:
the *decision* (which ask, if any) is computed here from per-user cadence state;
the planner only chooses whether to surface it via ``offer_feedback``.
"""
from __future__ import annotations

import logging
import os

from app.pipeline.context import PipelineContext

logger = logging.getLogger(__name__)


def maybe_set_feedback_signal(ctx: PipelineContext) -> None:
    """Compute the cadence signal for this turn and stash it on
    ``ctx.feedback_signal``. Runs once per turn. Fully self-contained and
    guarded by the caller; gated by ``FEEDBACK_PERIODIC_ENABLED`` (default on).

    Inputs it can't cheaply obtain at plan-time (thread turn-count, last-turn
    qc, just-rated) default to the conservative value so the ask never
    over-fires; wiring those in is a follow-up (spec §12)."""
    if (os.environ.get("FEEDBACK_PERIODIC_ENABLED", "1") or "1").strip().lower() in ("0", "false", "no"):
        return
    user_id = getattr(ctx, "user_id", None)
    if not user_id:
        return
    from app.storage import product_feedback as _pf

    state = _pf.get_prompt_state(user_id)
    # Real completed-turn count so the CSAT/NPS gates can actually fire (they
    # require a substantive thread). Falls back to a ctx hint, then 1.
    thread_turns = (
        _pf.get_thread_turn_count(getattr(ctx, "thread_id", None))
        or int(getattr(ctx, "thread_turn_count", 0) or 0)
        or 1
    )
    signal = _pf.evaluate_cadence(
        state,
        user_id=user_id,
        thread_turns=thread_turns,
        last_turn_failed=False,
        nudged_this_thread=False,
        just_rated=False,
    )
    if signal:
        ctx.feedback_signal = signal
    # Advance the open-periodic clock. The frontend resets it via the
    # /event "shown" → mark_prompted path once a chip is actually surfaced.
    _pf.bump_counters(user_id, turns=1)


def enrich_offer_feedback(offer: dict) -> dict:
    """Add the display text + score scale the frontend widget renders. Keeps
    the planner's decision (just `kind`) minimal; display config lives here.
    csat/nps → a score widget that POSTs /chat/product-feedback/score;
    generic/targeted_miss → a chip that opens the capture form."""
    kind = (offer or {}).get("kind") or "generic"
    out = dict(offer or {})
    if kind == "nps":
        out.update(survey_type="nps", prompt="How likely are you to recommend Mobius to a colleague?",
                   scale={"min": 0, "max": 10, "min_label": "Not likely", "max_label": "Very likely"},
                   post_to="/chat/product-feedback/score")
    elif kind == "csat":
        out.update(survey_type="csat", prompt="How did that go?",
                   scale={"min": 1, "max": 5, "min_label": "Poor", "max_label": "Great"},
                   post_to="/chat/product-feedback/score")
    elif kind == "targeted_miss":
        out.update(prompt="That one missed — mind telling us what you expected?",
                   cta="Tell us", post_to="/chat/product-feedback")
    else:
        out.update(prompt="Anything about Mobius you'd change?",
                   cta="Share feedback", post_to="/chat/product-feedback")
    return out
