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

import contextvars
import logging
import re
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
# Time spent GETTING to the database, before the caller's SQL runs: pool
# getconn, its SELECT 1 liveness probe, and the commit that follows. Separate
# from db.read/db.write on purpose — it is acquire-time wearing query-time's
# clothes, and a count keyed on the target table cannot see it, because it is
# not a read of that table, it is the cost of reaching it. Four reads reporting
# n=1 each while the wall said 1.2s is exactly what that looks like from the
# outside.
KIND_DB_ACQUIRE = "db.acquire"
KIND_LLM = "llm"
KIND_HTTP = "http"

# Tool-selection counts. These three exist to separate the three layers that
# can drop a tool call, which today produce IDENTICAL evidence: none.
#
#   Ananth, live 2026-09-09: "can i offer some feedback" -> the model replied
#   that its feedback tool was broken. 73 seconds later, same session, the
#   tool fired fully. mobius-feedback /classify answers 200 in 1.3s. The tool
#   is not broken; it is intermittently NOT SELECTED.
#
# Three candidate layers, one tell each:
#   TOOL_OFFERED    the manifest actually rendered this turn. ctx.allowed_tools
#                   is mode- and subscription-filtered, so a tool can vanish
#                   between two turns with no error raised anywhere.
#   TOOL_EMITTED    every tool name the planner emitted, INCLUDING names that
#                   failed to parse. Parsing is deterministic, so a malformed
#                   emission is dropped rather than retried — and a drop with
#                   no record is indistinguishable from never emitting.
#   TOOL_DISPATCHED target carries "<tool>:<outcome>", so a dispatch that
#                   failed is distinguishable from one that never happened.
#
# A duration cannot tell these three apart. A count can, at n=1.
KIND_ROUND = "round"
KIND_TOOL_OFFERED = "tool.offered"
KIND_TOOL_EMITTED = "tool.emitted"
KIND_TOOL_DISPATCHED = "tool.dispatched"

_KINDS = {KIND_DB_READ, KIND_DB_WRITE, KIND_DB_ACQUIRE, KIND_LLM, KIND_HTTP, KIND_ROUND,
          KIND_TOOL_OFFERED, KIND_TOOL_EMITTED, KIND_TOOL_DISPATCHED}

# Span names are NODE KEYS from the chat schema, not ad-hoc labels.
#
# Ananth: "shouldn't the span also map to our schema in some regards.. else
# what is the point of those modules". The reason is not tidiness. Ad-hoc
# names give two independent decompositions of one system — 36 schema nodes
# and N arbitrary spans — and neither can check the other. Tie them and they
# become reciprocally falsifiable:
#
#   a span whose name is not a node  -> the schema is incomplete
#   a live node that never spans     -> it is dead code, or mis-modelled
#
# Today neither error is detectable: the schema is hand-written and nothing
# in the running system contradicts it. That is the same "nothing fails
# loudly when a producer disappears" shape this program has spent two days
# cataloguing, applied to our own map of the system.
#
# AUTHORITATIVE SOURCE is scripts/platform/chat_node_content.py in the parent
# repo, which mobius-chat cannot import. This copy is a runtime guard only;
# refresh.sh owns enforcement (fails when a span name is not a node, or a
# live node produces no span across a corpus run). Mismatch here logs, never
# raises — a telemetry guard must not be able to fail a turn.
NODE_KEYS = frozenset({
    "PHI gate", "POST /chat", "active_context", "capabilities", "clarification",
    "clarify", "classify", "completion_extension_gate", "context", "continuity",
    "credentialing_envelope", "critic", "curator_tools", "emit_envelope",
    "feedback_signal", "governor", "integrate", "jurisdiction", "llm_manager",
    "message_resolver", "orchestrator", "parsing", "personalization", "plan",
    "prompts", "queue", "react_loop", "react_retry_guard", "resolve",
    "retrieval_budget", "round0", "run_pipeline", "stages", "state_load",
    "tool_manifest", "worker",
})

# ── phases ───────────────────────────────────────────────────────────
#
# Ananth's framing: preprocessing · react · postprocessing, with the real
# modules bundled underneath, so a reader gets a phase-level answer first and
# drills into modules only when the phase is the suspect.
#
# The value is that the three phases have different levers. Preprocessing is
# almost all I/O — make fewer calls, or make them cheaper. React is dominated
# by model routing, which no refactor touches. Postprocessing is our own
# composition code. "The turn is slow" is unanswerable; "preprocessing is
# 40% of it" points at one of three different teams' work.
#
# Assignment is by WHERE IN THE TURN a node runs, not by what it is about.
# UNPHASED IS DELIBERATE AND VISIBLE: a node with no phase shows up in its own
# bucket rather than being defaulted into one. Silently bucketing an unmapped
# node would make the phase totals quietly wrong, which is worse than an
# obvious gap — the same reason an unknown root span is recorded verbatim
# rather than normalised.
PHASE_PRE = "preprocessing"
PHASE_REACT = "react"
PHASE_POST = "postprocessing"

# Schema nodes with NO code behind them (verified against git history).
# Such a node can never produce a span, so its absence from a trace says nothing
# about the turn — it is a stale schema entry, not a fast node. Naming it here
# is the difference between "measured zero" and "does not exist".
#
#   credentialing_envelope — module deleted by P1d (commit 298830c). Its core,
#       resolve_step3_roster_merge_context, has zero remaining references. The
#       SCHEMA still rates it green with a full description.
#
# CORRECTION: completion_extension_gate was in this set and should NOT have
# been. It is live code — roughly sixty lines INLINE in react_loop's main loop
# (the completion critic + extension-round bump), with no module, class or
# function of its own. My "does it exist" test was a search for a symbol, and a
# nameless block has no symbol, so absence of a match proved nothing. The schema
# was right; the search was wrong. It now carries an explicit span.
#
# The general rule this cost me: a name-based search answers "is there a symbol
# called X", never "does X happen". Reserve this set for absence confirmed
# against git history, not against grep.
NODES_WITHOUT_CODE = frozenset({"credentialing_envelope"})

NODE_PHASE: dict[str, str] = {
    # ── preprocessing: everything before the reasoning loop opens ──
    "POST /chat": PHASE_PRE, "queue": PHASE_PRE, "worker": PHASE_PRE,
    "run_pipeline": PHASE_PRE, "orchestrator": PHASE_PRE,
    "PHI gate": PHASE_PRE, "state_load": PHASE_PRE, "context": PHASE_PRE,
    "active_context": PHASE_PRE, "jurisdiction": PHASE_PRE,
    "personalization": PHASE_PRE, "message_resolver": PHASE_PRE,
    "capabilities": PHASE_PRE, "tool_manifest": PHASE_PRE,
    "clarification": PHASE_PRE, "clarify": PHASE_PRE, "classify": PHASE_PRE,
    "plan": PHASE_PRE, "stages": PHASE_PRE, "continuity": PHASE_PRE,
    # ── react: the reason -> act -> observe loop and its machinery ──
    "react_loop": PHASE_REACT, "round0": PHASE_REACT, "prompts": PHASE_REACT,
    "parsing": PHASE_REACT, "critic": PHASE_REACT, "governor": PHASE_REACT,
    "completion_extension_gate": PHASE_REACT, "react_retry_guard": PHASE_REACT,
    "retrieval_budget": PHASE_REACT, "curator_tools": PHASE_REACT,
    "llm_manager": PHASE_REACT, "resolve": PHASE_REACT,
    # ── postprocessing: turning the loop's output into the answer ──
    "integrate": PHASE_POST, "emit_envelope": PHASE_POST,
    "feedback_signal": PHASE_POST, "credentialing_envelope": PHASE_POST,
}

PHASE_ORDER = [PHASE_PRE, PHASE_REACT, PHASE_POST, "unphased"]


def phase_for(node: str) -> str:
    return NODE_PHASE.get(node, "unphased")


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
    module: str          # the NODE KEY (inherited by children finer than a node)
    depth: int
    started_at: float
    # Local label for a span finer than any node — a single tool call inside
    # react_loop, a single write inside state_load. The mapping is
    # node -> span TREE, not node -> span. A child whose parent is not a node
    # is still a schema gap, which is why `module` is inherited rather than
    # left blank.
    label: str | None = None
    wall_ms: float = 0.0
    # llm_ms is time spent in LLM calls attributed to THIS span, measured in
    # process. Deliberately not joined from llm_calls: 790 of 1,979 rows had
    # a NULL correlation_id as of 2026-09-09, so that join returns a partial
    # set with no signal that it is partial. Measuring in-span keeps the
    # number correct regardless of whether that table is ever fixed.
    llm_ms: float = 0.0
    # Ran in parallel with its siblings — see db/schema/064.
    concurrent: bool = False
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
            "label": self.label,
            "depth": self.depth,
            "wall_ms": round(self.wall_ms, 2),
            "llm_ms": round(self.llm_ms, 2),
            "self_ms": round(self.self_ms, 2),
            "concurrent": self.concurrent,
            "counts": [c.to_dict() for c in self.counts.values()],
        }


class TurnTrace:
    """All spans for one turn. Attach to ctx; pass explicitly."""

    def __init__(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id
        self.spans: list[Span] = []
        self._stack: list[Span] = []
        self._open_cms: list[Any] = []

    # ── recording ────────────────────────────────────────────────────
    def add_completed(self, module: str, label: str, wall_ms: float,
                      llm_ms: float = 0.0, kind: str | None = None,
                      target: str | None = None,
                      rollup_llm_ms: float | None = None,
                      concurrent: bool = False) -> None:
        """File an ALREADY-FINISHED span measured somewhere this stack cannot go.

        Concurrent fan-outs (the integrator's 3-way ThreadPoolExecutor) cannot
        use ``span()``: ``_stack`` is a plain list, so three threads pushing and
        popping it would mis-parent each other's spans and the tree would be
        quietly wrong — worse than absent. So the worker times itself, and the
        PARENT thread files the result here after the join, where the stack is
        single-threaded again. No push/pop: the span is born closed.
        """
        parent = self._stack[-1] if self._stack else None
        node = module
        if node not in NODE_KEYS and parent is not None:
            label, node = label or node, parent.module
        sp = Span(
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent.span_id if parent else None,
            module=node, label=label, depth=len(self._stack),
            started_at=time.perf_counter(),
            wall_ms=float(wall_ms), llm_ms=float(llm_ms),
            concurrent=bool(concurrent),
        )
        if kind:
            # The matrix reads its llm/db columns from COUNTS, not from
            # Span.llm_ms — so a span with llm_ms set but no count still shows
            # its model time as processing. Both, or the column lies.
            sp.counts[(kind, target or label)] = _Count(
                kind=kind, target=target or label, n=1, ms=float(llm_ms or wall_ms))
        self.spans.append(sp)
        # Roll up to ancestors, same as span() does on close. CONCURRENT
        # siblings must pass rollup_llm_ms explicitly: summing three parallel
        # calls would charge the parent more LLM time than it has wall, and the
        # parent's processing would clamp to zero — under-reporting our own code
        # instead of over-reporting it. The caller knows they overlapped; this
        # method cannot.
        roll = llm_ms if rollup_llm_ms is None else rollup_llm_ms
        if roll:
            for a in self._stack:
                a.llm_ms += float(roll)

    @contextmanager
    def span(self, module: str, label: str | None = None) -> Iterator[Span]:
        """Open a span named by a SCHEMA NODE KEY.

        A child span finer than any node passes ``label`` and inherits the
        parent's node key, so every span still resolves to a node and the
        span set stays checkable against the schema in both directions.
        """
        parent = self._stack[-1] if self._stack else None
        node = module
        if node not in NODE_KEYS:
            if parent is not None:
                # Finer than a node: inherit the parent's key, keep the name
                # as a local label so the detail is not lost.
                label = label or node
                node = parent.module
            else:
                # A ROOT span that is not a node means the schema is missing
                # something the system actually does. Loud, and recorded as
                # given so refresh.sh can surface the gap rather than have it
                # silently normalised away.
                logger.warning(
                    "[spans] root span %r is not a schema node key — either the "
                    "schema is incomplete or this span is misnamed (cid=%s)",
                    node, self.correlation_id[:8],
                )
        sp = Span(
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent.span_id if parent else None,
            module=node,
            label=label,
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

    # ── manual open/close ────────────────────────────────────────────
    # The context manager needs an indented block. react_loop's round body is
    # ~600 lines under `for iteration in count()`, and wrapping it would mean
    # re-indenting the largest module in the codebase for telemetry. These let
    # a caller open a span at the top of a loop body and close it at the top
    # of the next iteration, giving REAL spans — which matters because a span
    # collects llm/db counts from record_ambient, and a bare duration cannot.
    # That is the difference between "round 2 took 11s" and "round 2 took 11s,
    # of which 9s was llm and 300ms was db".
    def open_span(self, module: str, label: str | None = None) -> Span:
        cm = self.span(module, label=label)
        sp = cm.__enter__()
        self._open_cms.append(cm)
        return sp

    def close_span(self) -> None:
        if not self._open_cms:
            return
        cm = self._open_cms.pop()
        try:
            cm.__exit__(None, None, None)
        except Exception as exc:  # pragma: no cover
            logger.warning("[spans] close_span failed: %s", exc)

    def close_all(self) -> None:
        """Close every still-open span, innermost first.

        The turn root is opened in run_pipeline and closed at persist time, in
        a different function, and any stage that returned early (clarification,
        refusal) leaves its own spans open too. An unclosed span has wall_ms 0,
        so it would render as a node that took no time — the same "0 means not
        measured" lie this whole exercise is about removing.
        """
        while self._open_cms:
            self.close_span()

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
def span(ctx: Any, module: str, label: str | None = None) -> Iterator[Span | None]:
    """ctx-aware span. No-ops when the turn has no trace attached.

    Lets call sites be instrumented without every one of them branching on
    whether tracing is on.
    """
    tr = get_trace(ctx)
    if tr is None:
        yield None
        return
    with tr.span(module, label=label) as sp:
        yield sp


def record(ctx: Any, kind: str, target: str, n: int = 1, ms: float = 0.0) -> None:
    tr = get_trace(ctx)
    if tr is not None:
        tr.record(kind, target, n=n, ms=ms)


# ── ambient trace ────────────────────────────────────────────────────
#
# The header above says spans are passed explicitly, not held in a
# thread-local, so a span crossing a thread boundary is a visible choice.
# That still holds for SPANS. It does not work for COUNTS: an LLM call in
# llm_manager and a query in db_client are many frames below the pipeline
# and have no ctx, and threading ctx through both is a large, invasive
# change to modules this phase is not refactoring.
#
# A ContextVar is the right tool rather than a compromise:
#   * it is per-logical-context, so concurrent turns cannot cross-talk
#   * it does NOT propagate into new threads by default, so react_loop's
#     daemon threads (e.g. the RAG grade callback) cannot silently attach
#     counts to a turn that has already completed — the exact mis-parenting
#     the explicit rule was written to prevent
#
# So: spans stay explicit; ambient counts attach to whatever turn is
# actually executing. If no turn is active, record_ambient is a no-op and
# says nothing — an LLM call outside a turn (a warm-up, an eval) is not an
# error and must not log on every occurrence.
_ACTIVE: contextvars.ContextVar["TurnTrace | None"] = contextvars.ContextVar(
    "mobius_active_turn_trace", default=None
)


def set_active(trace: "TurnTrace | None"):
    """Bind the turn's trace to this execution context. Returns a reset token."""
    return _ACTIVE.set(trace)


def reset_active(token) -> None:
    try:
        _ACTIVE.reset(token)
    except Exception:  # pragma: no cover — token from another context
        _ACTIVE.set(None)


# Measurements that arrived with no active trace to hold them. This is not
# bookkeeping trivia: llm_manager.generate() reports EVERY model call through
# record_ambient, and _ACTIVE deliberately does not cross a thread boundary, so
# a call made from a worker pool used to vanish here in total silence. Its time
# then reappeared as the caller's `processing` — that is exactly how 11.5s of
# integrator model wait was read as 12s of our own code. A dropped measurement
# must be countable, or the fallback attribution stays plausible forever.
_ORPHANED: dict[str, float] = {}


def orphaned_ms() -> dict[str, float]:
    """Time that had no span AT RECORD TIME, by kind, for this process's life.

    An UPPER BOUND on loss, not a loss figure: a pool worker's call orphans here
    and is then filed by its parent after the join, so the integrator's three
    calls land in this tally every turn despite being correctly attributed.
    Treat a rise as "go look at that turn's matrix", not as proof of a gap.
    """
    return dict(_ORPHANED)


def record_ambient(kind: str, target: str, n: int = 1, ms: float = 0.0) -> None:
    """Record against the active turn. A drop is counted, never silent."""
    tr = _ACTIVE.get()
    if tr is None or not tr._stack:
        if ms:
            _ORPHANED[kind] = _ORPHANED.get(kind, 0.0) + float(ms)
            # Only LLM drops are warned about. Not every orphan is a defect:
            # the progress writer and other daemon threads run OUTSIDE any turn
            # by design (that is why _ACTIVE does not propagate — so a daemon
            # cannot attach counts to a turn that already finished), and they
            # produce a steady stream of orphaned db writes. Warning on those
            # would be noise that trains a reader to ignore the line, taking
            # the real signal with it.
            #
            # An orphaned LLM call is different: every model call belongs to
            # some turn, so one with no span is a measurement that has gone
            # missing, and its time WILL surface as its caller's processing.
            # That is the 11.5s-as-our-code failure, and it should be loud.
            if kind == KIND_LLM:
                # Deliberately hedged. A call made in a pool worker orphans HERE
                # and is still filed correctly a moment later by the parent
                # after the join (add_completed) — the integrator fan-out does
                # exactly that, three times a turn. Saying "NOT attributed"
                # flatly would cry wolf on the one path already fixed, and a
                # warning that is wrong three times a turn is a warning nobody
                # reads by the fourth. Check the turn's matrix before treating
                # one of these as a real loss.
                logger.warning(
                    "[spans] llm %.0fms on %r had no active span AT RECORD TIME. "
                    "If the caller files it after a join it is still counted "
                    "(integrator fan-out does); otherwise this time is lost and "
                    "will look like the caller's own processing.",
                    ms, target,
                )
            else:
                logger.debug("[spans] orphaned %s %.0fms on %r", kind, ms, target)
        return
    try:
        tr.record(kind, target, n=n, ms=ms)
    except Exception as exc:  # never let telemetry break a caller
        logger.warning("[spans] record_ambient(%s,%s) failed: %s", kind, target, exc)


_TABLE_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_.]*)",
    re.IGNORECASE,
)


def sql_target(sql: str) -> str:
    """Best-effort table name from a SQL statement.

    The TARGET is the point: "40 writes to chat_state" is a loop, "40 writes
    across 40 tables" is a busy turn, and a bare count cannot separate them.
    Falls back to "unknown_table" rather than dropping the count — an
    unattributed write is still evidence of a write.
    """
    m = _TABLE_RE.search(sql or "")
    return m.group(1).lower() if m else "unknown_table"


# ── sampling ─────────────────────────────────────────────────────────
#
# Telemetry costs a DB write per span per turn. At full rate that is fine
# now and will not be at volume, so the gate exists before it is needed
# rather than after the bill.
#
# DETERMINISTIC, NOT RANDOM. The sample decision is a hash of the
# correlation_id, so:
#   * the same turn is always sampled or always not — re-running a trace
#     for the same cid gives the same answer, which random sampling cannot
#   * it can be LINED UP WITH THE QA AUDIT RUNS: if the audit selects turns
#     by cid, telemetry can be made to select exactly the same set instead
#     of an independent random subset that happens to overlap. Two
#     independent samples of the same population answer different questions.
#
# Default rate is 1.0 — every turn — per Ananth 2026-09-09: run everywhere
# for now, narrow it to the audit set once that cadence is fixed.
#
# CRITICALLY, the decision is RECORDED on the turn. Without it, "this turn
# has no spans" means both "not sampled" and "sampled but recorded nothing",
# and those are opposite conclusions — one is expected, the other is a bug.
# That ambiguity is the same absence-with-no-record shape __none__ and
# __unfiltered__ exist to prevent.
import hashlib
import os


def sample_rate() -> float:
    raw = (os.environ.get("MOBIUS_SPAN_SAMPLE_RATE") or "1.0").strip()
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        logger.warning("[spans] bad MOBIUS_SPAN_SAMPLE_RATE=%r — defaulting to 1.0", raw)
        return 1.0


def is_sampled(correlation_id: str, rate: float | None = None) -> bool:
    r = sample_rate() if rate is None else rate
    if r >= 1.0:
        return True
    if r <= 0.0:
        return False
    # Uniform in [0,1) from the cid — stable across processes and restarts.
    h = hashlib.sha256((correlation_id or "").encode("utf-8")).digest()
    bucket = int.from_bytes(h[:8], "big") / float(1 << 64)
    return bucket < r


# ── turn source ──────────────────────────────────────────────────────
SOURCE_REAL = "real"
SOURCE_SMOKE = "smoke"
SOURCE_EVAL = "eval"

_SMOKE_CID_PREFIXES = ("sel-cid-", "smoke-", "probe-", "test-cid-", "cid-")


def classify_source(correlation_id: str, declared: str | None = None) -> str:
    """Which population this turn belongs to.

    Fleet percentiles are only meaningful over one population. Deploy smoke
    turns hit cold connection pools at ~400-500ms per DB call while real
    turns run at ~30-39ms; mixed, the p50 describes neither.

    `declared` wins when a caller states its source (an eval harness knows
    what it is). Otherwise infer: chat mints real correlation_ids as UUID4,
    so anything that is NOT a UUID was hand-made by a script — the smoke
    probe, a trace, a local repro.

    Errs toward SMOKE for unrecognised shapes rather than REAL: polluting
    the real-traffic baseline with synthetic turns is the failure that
    matters, and a synthetic turn wrongly excluded is merely absent.
    """
    if declared in (SOURCE_REAL, SOURCE_SMOKE, SOURCE_EVAL):
        return declared
    cid = (correlation_id or "").strip()
    low = cid.lower()
    if any(low.startswith(p) for p in _SMOKE_CID_PREFIXES):
        return SOURCE_SMOKE
    try:
        uuid.UUID(cid)
        return SOURCE_REAL
    except (ValueError, AttributeError, TypeError):
        return SOURCE_SMOKE


def mark_round(ctx: Any, rn: int | None) -> None:
    """Close the previous ReAct round's span and open the next.

    Rounds are REAL SPANS, not bare durations. A span collects the llm and db
    counts that record_ambient attributes to the open stack top, so a round
    row can say "11.3s, of which 9.4s llm and 0.3s db" instead of just 11.3s.
    A duration alone cannot be decomposed after the fact.

    Opened/closed manually rather than with a `with` block because the round
    body is ~600 lines under `for iteration in count()` and wrapping it would
    mean re-indenting the largest module in the codebase for telemetry. The
    boundary marks are equivalent; only the syntax differs.
    """
    tr = get_trace(ctx)
    if tr is None:
        return
    if getattr(ctx, "_span_round_open", False):
        tr.close_span()
        ctx._span_round_open = False
    if rn is not None:
        tr.open_span("react_loop", label=f"round_{rn}")
        ctx._span_round_open = True


def close_rounds(ctx: Any) -> None:
    """Close the final round, which has no successor to close it."""
    mark_round(ctx, None)


def classify_source(correlation_id: str, declared: str | None = None) -> str:
    """Which population this turn belongs to.

    Fleet percentiles are only meaningful over one population. Deploy smoke
    turns hit cold connection pools at ~400-500ms per DB call while real
    turns run at ~30-39ms; mixed, the p50 describes neither.

    `declared` wins when a caller states its source (an eval harness knows
    what it is). Otherwise infer: chat mints real correlation_ids as UUID4,
    so anything that is NOT a UUID was hand-made by a script — the smoke
    probe, a trace, a local repro.

    Errs toward SMOKE for unrecognised shapes rather than REAL: polluting
    the real-traffic baseline with synthetic turns is the failure that
    matters, and a synthetic turn wrongly excluded is merely absent.
    """
    if declared in (SOURCE_REAL, SOURCE_SMOKE, SOURCE_EVAL):
        return declared
    cid = (correlation_id or "").strip()
    low = cid.lower()
    if any(low.startswith(p) for p in _SMOKE_CID_PREFIXES):
        return SOURCE_SMOKE
    try:
        uuid.UUID(cid)
        return SOURCE_REAL
    except (ValueError, AttributeError, TypeError):
        return SOURCE_SMOKE






_TRACED_NO_CTX_WARNED: set = set()


def traced(node: str, label: str | None = None):
    """Decorator: run a function inside a span on `node`.

    USES THE AMBIENT TRACE, not a ctx argument. The first version sniffed the
    arguments for a PipelineContext and, not finding one, returned the
    function unwrapped — SILENTLY. Four of the six functions instrumented that
    way (governor.evaluate, should_run_critic, parse_critic_response,
    resolve_pronouns) take no ctx, so they were decorated, recorded nothing,
    and were indistinguishable in the matrix from modules that never ran.

    A decorator that silently declines to instrument is the
    producer-without-a-consumer defect wearing the instrument's own clothes —
    the third time in this build I have written it. The ambient ContextVar
    removes the requirement entirely: any function executing inside a turn
    gets a span, whatever its signature.

    No active trace is a genuine no-op and stays silent: a function called
    outside a turn (a warm-up, an eval, a unit test) is not an error and must
    not log on every call.
    """
    def _wrap(fn):
        import functools

        @functools.wraps(fn)
        def _inner(*args, **kwargs):
            tr = _ACTIVE.get()
            if tr is None or not tr._stack:
                return fn(*args, **kwargs)
            with tr.span(node, label=label or fn.__name__):
                return fn(*args, **kwargs)
        return _inner
    return _wrap
