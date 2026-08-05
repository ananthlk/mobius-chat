"""ReAct Round 0 — system_context short-circuit.

When ``POST /chat`` supplies ``system_context`` — pre-loaded, verified data
from a caller that already did the work (story presentation nodes, skill
cards with computed metrics) — we try to answer directly from that context
before entering the normal Round 1..N tool loop.

Second caller (2026-08-04, Task #29,
docs/MIDTURN_TRUNCATION_RECOVERY_SPEC.md §3): a "Continue" retry after a
truncated turn is just a new ``POST /chat`` with ``system_context`` set to
the checkpointed evidence (see ``react_loop.py``'s
``_checkpoint_best_evidence``). No react-side code change was needed for
this reuse — ``sys_ctx`` flows into ``build_round0_user_message``
unmodified regardless of which caller supplied it, so a checkpoint gets
exactly the same "is this sufficient, or NEEDS_TOOLS" evaluation any other
pre-loaded context gets. Locked in by
``test_round0_accepts_checkpoint_evidence_as_system_context`` in
tests/test_system_context.py.

Prompt contract
---------------
The LLM either returns a full answer (context sufficient), or returns
exactly ``NEEDS_TOOLS`` (context insufficient, fall through to tools).
We accept either the bare sentinel or a short ``NEEDS_TOOLS: reason``
form.

Cost / latency
--------------
~1 LLM call vs. 3–5 in a typical ReAct loop, with no tool invocations.
For the story layer (which knows the data is fully there), this is the
common case.

Integration
-----------
``run_react()`` in :mod:`app.pipeline.react_loop` calls
:func:`try_system_context_round0` before its iteration loop. Returns
True → run_react returns immediately. Returns False → normal loop runs,
and :mod:`react_loop` prepends the same SYSTEM_CONTEXT block to every
round's reasoning context so tools complement rather than re-derive.
"""
from __future__ import annotations

import logging
from typing import Callable

from app.pipeline.context import PipelineContext
from app.services.doc_assembly import RETRIEVAL_SIGNAL_SYSTEM_CONTEXT

logger = logging.getLogger(__name__)


ROUND0_SENTINEL = "NEEDS_TOOLS"
"""Sentinel returned by the LLM when the system_context is insufficient.

We accept the bare token or a ``NEEDS_TOOLS: <reason>`` prefix form."""


ROUND0_SYSTEM_PROMPT = (
    "You are answering a user's question using ONLY the pre-loaded context "
    "below, which is verified ground truth. Do not search, do not speculate, "
    "do not use external knowledge.\n\n"
    "IMPORTANT: The pre-loaded context is a high-level summary (aggregate entity "
    "totals). Richer detail may be available via other tools in the next "
    "rounds — prefer routing to them for anything beyond simple aggregates.\n\n"
    "Decision rule:\n"
    "  - Only answer from context if the question can be fully answered by the "
    "aggregate numbers already present (e.g. 'what is BHPF's total market share?').\n"
    "  - For ANY question that needs org-level detail, rankings, individual org data, "
    "trends beyond what's shown, or comparisons between orgs — return "
    f"exactly: {ROUND0_SENTINEL}\n\n"
    "ALWAYS return NEEDS_TOOLS when the question:\n"
    "  - Asks for a breakdown or list by individual organization\n"
    "  - Asks 'which organization', 'which org', 'by org', 'per org', 'each org'\n"
    "  - Asks about a specific named org (e.g. 'Aspire', 'BayCare', 'Henderson')\n"
    "  - Asks about new entrants, specific new orgs, who entered when\n"
    "  - Asks for rate benchmarks, HCPCS rates, billing codes, or fee schedule rates\n"
    "  - Asks what FL Medicaid pays, published rates, or the ceiling rate for a code\n"
    "  - Asks for deeper detail beyond the summary numbers in context\n\n"
    "Do not wrap your answer in JSON. Return plain prose for the answer, or "
    f"the bare token {ROUND0_SENTINEL} when tools would do better."
)


def build_round0_user_message(system_context: str, question: str) -> str:
    """Compose the user message shown to the LLM for Round 0.

    Exposed so callers (and tests) can inspect the exact prompt without
    calling the LLM."""
    return (
        f"[SYSTEM CONTEXT — treat as verified data, do not search for this]\n"
        f"{system_context}\n"
        f"[END SYSTEM CONTEXT]\n\n"
        f"Question: {question}"
    )


def build_round_context_prefix(system_context: str) -> str:
    """Build the SYSTEM CONTEXT block that gets prepended to every ReAct
    round's reasoning context when Round 0 falls through to tools.

    Keeping this here (rather than inline in react_loop) gives Round 0
    and the fallthrough path a single source of truth for how the
    context is labeled to the model."""
    return (
        f"[SYSTEM CONTEXT — treat as verified data, do not search for this]\n"
        f"{system_context}\n"
        f"[END SYSTEM CONTEXT]\n\n"
    )


def _is_needs_tools(text: str) -> bool:
    """Accept bare sentinel or ``NEEDS_TOOLS: reason`` prefix."""
    head = (text or "").strip().split("\n", 1)[0].strip()
    return head == ROUND0_SENTINEL or head.upper().startswith(ROUND0_SENTINEL)


def try_system_context_round0(
    ctx: PipelineContext,
    emitter: Callable[[str], None] | None = None,
    *,
    llm_caller: Callable[..., str] | None = None,
    finalizer: Callable[..., None] | None = None,
) -> bool:
    """Attempt a context-grounded answer from ``ctx.system_context``.

    Returns True when Round 0 produced a complete answer and the caller
    should skip the normal tool loop. Returns False when the model
    signaled NEEDS_TOOLS, the attempt itself failed, or preconditions
    weren't met (no system_context, no question) — in all those cases
    the caller should proceed with the normal ReAct loop.

    On success, finalizes ``ctx`` via ``finalizer`` with:
      - signal = ``RETRIEVAL_SIGNAL_SYSTEM_CONTEXT``
      - sources = [] (answer grounded in caller-supplied data)
      - last_tool = ``"system_context"``
      - ``ctx.react_rounds_used = 0`` (analytics: short-circuit path)

    ``llm_caller`` and ``finalizer`` are injected for testability; when
    None (the production path) they're imported lazily from
    ``react.prompts`` and ``react_loop`` respectively to avoid an import
    cycle at module load.
    """
    def _emit(msg: str) -> None:
        if emitter and msg:
            emitter(str(msg).strip())

    sys_ctx = (getattr(ctx, "system_context", None) or "").strip()
    if not sys_ctx:
        return False

    question = (ctx.effective_message or ctx.message or "").strip()
    if not question:
        return False

    # Lazy imports: prompts → react_loop has a cycle if done at module top.
    if llm_caller is None:
        from app.pipeline.react.prompts import _call_llm_json as llm_caller  # type: ignore
    if finalizer is None:
        from app.pipeline.react_loop import _finalize_response as finalizer  # type: ignore

    _emit("◌ Checking the pre-loaded context first…")

    user_msg = build_round0_user_message(sys_ctx, question)

    try:
        raw = llm_caller(
            ROUND0_SYSTEM_PROMPT,
            user_msg,
            max_tokens=800,
            ctx=ctx,
            stage="react_0",
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Round 0 LLM call failed, falling through to tools: %s", exc)
        _emit("  Context pre-check failed — continuing with normal reasoning.")
        return False

    text = (raw or "").strip()
    if not text:
        return False

    if _is_needs_tools(text):
        _emit("  Context insufficient — running full reasoning loop.")
        return False

    _emit("  Answered from pre-loaded context.")
    finalizer(
        ctx,
        text,
        [],  # no citations — data was pre-verified by caller
        RETRIEVAL_SIGNAL_SYSTEM_CONTEXT,
        "system_context",
        emitter,
    )
    ctx.react_rounds_used = 0
    return True
