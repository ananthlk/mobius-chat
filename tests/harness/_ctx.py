"""Minimal PipelineContext factory for harness scenarios.

The DB seat ruling: chat_state is mutated in place with no history, so
the state a turn actually ran with is unrecoverable.  Both PRE and POST
sides of the gate receive the same reconstructed ctx — that's the fidelity
ceiling and it's sufficient for structural invariant checking.

Fields we can't reconstruct (and don't try to):
  - merged_state (thread history prior turns)
  - blueprint / plan_snapshot
  - blueprint_snapshot  (0 of 2,744 rows; always NULL)
  - actual user message content (substituted with a neutral fixture question)

Fields we DO set per scenario:
  - chat_mode
  - context_summary (controls stratum: with_context vs no_context)
  - effective_message, message
"""
from __future__ import annotations

from app.pipeline.context import PipelineContext


def make_ctx(
    *,
    mode: str = "copilot",
    context_summary: str | None = None,
    message: str = "What are the prior authorization requirements for ABA therapy?",
    correlation_id: str = "harness-cid-00000000",
    thread_id: str | None = None,
) -> PipelineContext:
    """Build a minimal PipelineContext for harness scenario execution."""
    ctx = PipelineContext(
        correlation_id=correlation_id,
        thread_id=thread_id,
        message=message,
    )
    ctx.chat_mode = mode
    ctx.effective_message = message
    ctx.merged_state = {}
    ctx.last_turns = []

    # context_summary controls the with_context / no_context stratum
    # (1,853 / 897 split in the 2,750-turn baseline).
    ctx.context_summary = context_summary

    return ctx
