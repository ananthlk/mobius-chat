"""Invariant extractors — read a PipelineContext after run_react() and
return the values the gate assertions compare.

All extractors are pure functions over ctx; none touch the DB.

Invariants assertable today (I3/I5 deferred):

    I1  exactly one turn_completed envelope per turn
    I2  integrator bypass set matches expected
    I4  rounds_used and max_rounds match expected
    I7  log-and-continue count — static, see test_i7_swallow_count
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RunResult:
    """Extracted invariant values from one run_react() call."""

    # I1 — terminal envelope
    terminal_envelope_count: int

    # I2 — integrator bypass
    bypass_integrate: bool

    # I4 — rounds arithmetic
    rounds_used: int
    max_rounds: int | None

    # Bonus fields (not invariant assertions, but useful for diagnostics)
    final_directive: str | None
    unfinished_reason: str | None
    floor_ran: bool


def extract(ctx: Any) -> RunResult:
    """Extract invariant values from a PipelineContext after run_react().

    I1 proxy at the run_react boundary: we look for the react_trace
    envelope (signal="react_trace"), not turn_completed — the latter is
    emitted by the orchestrator, which we do not invoke in harness tests.
    One react_trace per run_react() call is the invariant.
    """
    chunks = getattr(ctx, "thinking_chunks", None) or []
    terminal_count = sum(
        1 for c in chunks
        if isinstance(c, dict) and c.get("signal") == "react_trace"
    )
    return RunResult(
        terminal_envelope_count=terminal_count,
        bypass_integrate=bool(getattr(ctx, "react_bypass_integrate", False)),
        rounds_used=int(getattr(ctx, "react_rounds_used", 0) or 0),
        max_rounds=getattr(ctx, "react_max_rounds", None),
        final_directive=getattr(ctx, "product_promise_directive", None),
        unfinished_reason=getattr(ctx, "react_unfinished_reason", None),
        floor_ran=bool(getattr(ctx, "react_groundedness_floor_ran", False)),
    )
