"""Per-module turn spans: wall/llm split + counts labelled by target.

WHY THIS SHAPE (2026-09-09, P2b). Ananth's ask was to catch **bad writes,
bad DB calls and loops**, and not to drift. Three design consequences, each
of which a simpler schema would have foreclosed rather than merely blurred:

1. PARENT/CHILD, NOT A FLAT DICT.
   A flat per-module total cannot express repetition. Forty writes inside
   one module sum to one large number that is indistinguishable from a
   single slow call. The thing being hunted IS repetition, so the schema
   has to be able to hold it.

2. COUNTS CARRY A TARGET, NOT A BARE INTEGER.
   "40 writes" is ambiguous: 40 writes to `chat_state` is a loop; 40 writes
   across 40 tables is a busy turn. The integer alone cannot separate them
   and the loop is the stated target, so every count records what it hit.

3. n IS PRIMARY, NOT AN ADDENDUM.
   Duration answers "how long"; n answers "why". Counts are near
   deterministic — a loop is 40 writes on every run, a stray Pro call is +1
   on every run — so BOTH target bug classes show at a single run with no
   statistics. Wall time is confirmation; the count is the detector.

WHAT THIS REPLACES. `orchestrator._pf()` measures a step, logger.info()s it
when it exceeds 50ms, and stops. Nothing stores it, nothing reads it, and
sub-50ms steps are dropped — so forty 2ms writes are individually filtered
out AND never counted. The bug class this package targets is invisible to
the tooling that preceded it.

FAILURE POLICY. Recording must never break a turn, and must never fail
silently either — a telemetry layer that quietly stops recording is exactly
the producer-without-a-consumer defect it exists to detect. Every swallowed
error logs at WARNING naming the span, so a gap in the data has a
corresponding line in the logs rather than looking like a fast turn.

THREADING. TurnTrace is attached to the PipelineContext and passed
explicitly rather than held in a thread-local. react_loop spawns daemon
threads (e.g. the RAG grade callback); an implicit stack would either
mis-parent their spans or lose them. Explicit passing makes a span that
crosses a thread boundary a visible choice.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Count kinds. Deliberately small and closed — an open vocabulary would let
# call sites invent labels the reader does not render, which is how a
# producer loses its consumer one string at a time.
KIND_DB_READ = "db.read"
KIND_DB_WRITE = "db.write"
KIND_LLM = "llm"
KIND_HTTP = "http"
_KINDS = {KIND_DB_READ, KIND_DB_WRITE, KIND_LLM, KIND_HTTP}


@dataclass
class _Count:
    """One (kind, target) tally inside a span.

    ``ms`` is cumulative across the n occurrences, so `n=40, ms=80` reads as
    forty 2ms writes — the loop signature — and is distinguishable from
    `n=1, ms=80`, one slow call. That distinction is the whole point.
    """

    kind: str
    target: str
    n: int = 0
    ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.target,
                "n": self.n, "ms": round(self.ms, 2)}


@dataclass
class Span:
    span_id: str
    parent_span_id: str | None
    module: str
    depth: int
    started_at: float
    wall_ms: float = 0.0
    # llm_ms is time spent in LLM calls attributed to THIS span, measured in
    # process. Deliberately not joined from llm_calls: 790 of 1,979 rows had
    # a NULL correlation_id as of 2026-09-09, so that join returns a partial
    # set with no signal that it is partial. Measuring in-span keeps the
    # number correct regardless of whether that table is ever fixed.
    llm_ms: float = 0.0
    counts: dict[tuple[str, str], _Count] = field(default_factory=dict)

    @property
    def self_ms(self) -> float:
        """Wall time minus LLM time — the in-process + DB portion.

        This is the number that moves when code gets slower, as opposed to
        when the model router picks Pro over flash. Keeping them separate is
        what makes a regression attributable instead of noise.
        """
        return max(0.0, self.wall_ms - self.llm_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "module": self.module,
            "depth": self.depth,
            "wall_ms": round(self.wall_ms, 2),
            "llm_ms": round(self.llm_ms, 2),
            "self_ms": round(self.self_ms, 2),
            "counts": [c.to_dict() for c in self.counts.values()],
        }


class TurnTrace:
    """All spans for one turn. Attach to ctx; pass explicitly."""

    def __init__(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id
        self.spans: list[Span] = []
        self._stack: list[Span] = []

    # ── recording ────────────────────────────────────────────────────
    @contextmanager
    def span(self, module: str) -> Iterator[Span]:
        """Open a span. Nests under whatever span is currently open."""
        parent = self._stack[-1] if self._stack else None
        sp = Span(
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent.span_id if parent else None,
            module=module,
            depth=len(self._stack),
            started_at=time.perf_counter(),
        )
        self.spans.append(sp)
        self._stack.append(sp)
        try:
            yield sp
        finally:
            # finally, not else: a span that raised still has a duration, and
            # a turn that errored is exactly when the timing is interesting.
            sp.wall_ms = (time.perf_counter() - sp.started_at) * 1000.0
            if self._stack and self._stack[-1] is sp:
                self._stack.pop()
            else:  # pragma: no cover — indicates misuse, not a normal path
                logger.warning(
                    "[spans] stack corrupted closing module=%s cid=%s; "
                    "spans after this point may be mis-parented",
                    module, self.correlation_id[:8],
                )
                if sp in self._stack:
                    self._stack.remove(sp)
            # Roll LLM time up to the parent: a parent's llm_ms should include
            # its children's, so self_ms is honest at every level.
            if parent is not None:
                parent.llm_ms += sp.llm_ms

    def record(self, kind: str, target: str, n: int = 1, ms: float = 0.0) -> None:
        """Tally n occurrences of `kind` against `target` on the open span."""
        if not self._stack:
            # No open span. Loud, not silent: a count with nowhere to go is a
            # missing span, and a quiet drop here would make the instrument
            # under-report exactly when instrumentation is incomplete.
            logger.warning(
                "[spans] record(%s,%s) outside any span cid=%s — count dropped",
                kind, target, self.correlation_id[:8],
            )
            return
        if kind not in _KINDS:
            logger.warning("[spans] unknown kind=%r target=%r — recorded anyway", kind, target)
        sp = self._stack[-1]
        key = (kind, target)
        c = sp.counts.get(key)
        if c is None:
            c = _Count(kind=kind, target=target)
            sp.counts[key] = c
        c.n += n
        c.ms += ms
        if kind == KIND_LLM:
            sp.llm_ms += ms

    # ── reading ──────────────────────────────────────────────────────
    def to_rows(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.spans]

    def totals(self) -> dict[str, Any]:
        """Turn-level rollup. Depth-0 spans only, so nesting isn't double counted."""
        roots = [s for s in self.spans if s.depth == 0]
        by_target: dict[tuple[str, str], _Count] = {}
        for s in self.spans:
            for (kind, target), c in s.counts.items():
                agg = by_target.get((kind, target))
                if agg is None:
                    agg = _Count(kind=kind, target=target)
                    by_target[(kind, target)] = agg
                agg.n += c.n
                agg.ms += c.ms
        return {
            "wall_ms": round(sum(s.wall_ms for s in roots), 2),
            "llm_ms": round(sum(s.llm_ms for s in roots), 2),
            "span_count": len(self.spans),
            "counts": [c.to_dict() for c in by_target.values()],
        }


def get_trace(ctx: Any) -> TurnTrace | None:
    return getattr(ctx, "turn_trace", None)


@contextmanager
def span(ctx: Any, module: str) -> Iterator[Span | None]:
    """ctx-aware span. No-ops when the turn has no trace attached.

    Lets call sites be instrumented without every one of them branching on
    whether tracing is on.
    """
    tr = get_trace(ctx)
    if tr is None:
        yield None
        return
    with tr.span(module) as sp:
        yield sp


def record(ctx: Any, kind: str, target: str, n: int = 1, ms: float = 0.0) -> None:
    tr = get_trace(ctx)
    if tr is not None:
        tr.record(kind, target, n=n, ms=ms)
