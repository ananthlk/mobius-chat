"""Structured emit envelope — the typed shape of what the pipeline says it's doing.

**The problem this solves.** Before 2026-04-19, the ReAct loop + orchestrator
+ integrator emitted progress via bare strings:

    emit("◌ Searching our materials…")
    emit("⚠ Critic flagged 2 ungrounded claim(s); revising in next round.")
    emit("✓ Critic approved.")

Those strings landed in ``ctx.thinking_chunks: list[str]`` and were
persisted as a JSON array of strings in ``chat_turns.thinking_log``. The
UI rendered them as free-form lines. Analytics required regex-parsing
the strings — "count all rows where thinking_log contains 'Critic
flagged'." Fragile, ambiguous, and doesn't travel across systems.

The operator's framing: **events ARE tasks** the chat PM analyzes.
Failures, successes, blockers, rate failures, errors — these are
meaningful outcomes worth structured capture, not log spam.

**The envelope shape** matches task-manager's ``TaskSignalBody`` so
when we promote a subset of events to task-manager (Sprint A.2), the
payload round-trips cleanly without per-signal adapter logic.

    EmitEnvelope:
      signal:        str     -- semantic event name
      step_id:       str     -- hierarchical ID: "round_3.critic_audit"
      data:          dict    -- structured payload per signal
      note:          str     -- optional human-readable one-liner
      correlation_id: str
      thread_id:     str | None
      user_id:       str | None
      round:         int | None
      timestamp_ms:  int
      source_module: str     -- always "chat" here
      # Promotion metadata — consumed by Sprint A.2's writer.
      report_to_task_manager: bool
      task_type:     str | None   -- "failure" | "insight" | "blocker" | "info"
      task_severity: str | None   -- "low" | "med" | "high"

**Persistence.** Envelopes are serialized via ``to_dict()`` and stored
as JSON objects inside ``chat_turns.thinking_log`` (still a JSONB
array column; no migration needed). During the rollout period, the
array can mix legacy strings + new envelope dicts — ``is_envelope()``
distinguishes for the FE. Once every emit site is migrated, strings
stop appearing.

**Back-compat rendering.** Every envelope has a ``note`` field that is
the human-readable string the old code would have emitted. The FE
renders the note; the structured data enables richer UIs (icons per
signal, click-to-filter, etc.) as a later enhancement.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


# ── Signal taxonomy ──────────────────────────────────────────────────
#
# Curated for signal:noise. Chat-side-only signals (frequent, internal,
# derivable) aren't in this list — they're still emitted but via the
# generic ``note`` envelope with report_to_task_manager=False.
#
# The 10 analyzable signals below are the ones the chat PM queries
# from the task-manager dashboard once Sprint A.2 lands. Each mapping
# to task_type + severity is deliberate and documented at the
# constructor helper site.


Signal = Literal[
    # ── Promoted to task-manager (10 analyzable events) ──
    "turn_completed",                 # → info  (outcome + cost + rounds distribution)
    "turn_failed",                    # → failure high
    "tool_failed",                    # → failure med (non-recoverable only)
    "tool_exhausted",                 # → insight med
    "rate_limit_hit",                 # → failure high
    "rounds_exhausted_with_warning",  # → blocker high
    "critic_flagged",                 # → insight med
    "critic_approved_after_retry",    # → insight low
    "guidance_mode_activated",        # → insight low
    "confidence_filter_dropped_all",  # → insight low

    # ── Chat-side only (fine-grained trace) ──
    "turn_started",                   # fires every turn; derivable
    "round_started",                  # 3-6 per turn; noise
    "tool_called",                    # per-round; noise
    "tool_succeeded",                 # common case
    "tool_result_preview",            # debugging aid
    "critic_audit_started",           # internal, leads to flagged/approved
    "critic_approved",                # common case (no retry)
    "synthesizing_answer",            # UI decoration
    "integrator_validated",           # infrastructure
    "instant_rag_hit",                # derivable from turn_completed
    "mcp_skill_invoked",              # derivable from tool_called
    "healthcare_query_no_match",      # rare; kept chat-side for now
    "retrieval_trace",                # corpus_search skill telemetry — diagnostic UI panel
    "note",                           # generic fallback — plain-text emits migrated later

    # ── Thinking-chain trace (2026-05-04) ──────────────────────────────
    # User-visible progress steps that tell the story of how a question
    # was understood, searched, and evaluated. Two-tier rendering:
    #   ``note``  → plain-English line shown to all users
    #   ``data``  → structured payload surfaced in technical mode only
    "query_understood",               # question parsed into searchable intent
    "strategy_selected",              # search mode (auto/precision/recall) chosen
    "retrieval_complete",             # search round finished; N chunks returned
    "fallback_triggered",             # corpus missed; escalating to web / guidance
    "answer_quality",                 # adjudicator verdict (PASS/PARTIAL/FAIL)
]


# Task-manager types we route promoted events to. Matches the existing
# task_type enum in mobius-skills/task-manager.
TaskType = Literal["failure", "insight", "blocker", "info", "decision"]
TaskSeverity = Literal["low", "med", "high"]


# ── Envelope ─────────────────────────────────────────────────────────


@dataclass
class EmitEnvelope:
    """A single pipeline event, typed.

    Use the ``make_*`` helper constructors below rather than
    instantiating directly — the helpers ensure each signal gets the
    right task_type / severity / promotion flag. Direct construction
    should only happen for the generic ``note`` signal (back-compat
    wrapping of bare strings).
    """

    signal: Signal
    correlation_id: str
    step_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    note: str | None = None
    thread_id: str | None = None
    user_id: str | None = None
    round: int | None = None
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    source_module: str = "chat"
    # ── Promotion metadata ──
    # Sprint A.2 wires the writer that sees these flags. Sprint A.1
    # (this commit) just stores them; nothing reads them downstream
    # yet.
    report_to_task_manager: bool = False
    task_type: TaskType | None = None
    task_severity: TaskSeverity | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for storage in chat_turns.thinking_log."""
        d = asdict(self)
        # Prune None-valued optional fields so the JSON is compact.
        # The FE's is_envelope() detector keys on the "signal" field
        # being present; pruning doesn't affect detection.
        for k in ("note", "thread_id", "user_id", "round", "task_type", "task_severity"):
            if d.get(k) is None:
                del d[k]
        return d

    def render_for_ui(self) -> str:
        """Fallback renderer — produces the string the old UI code
        would have emitted. Used when the FE hasn't yet been upgraded
        to the structured renderer."""
        return self.note or f"[{self.signal}]"


def is_envelope(entry: Any) -> bool:
    """True when a thinking_log entry is an envelope dict (new shape)
    rather than a bare string (legacy).

    Detection is by shape — presence of a ``signal`` key on a dict.
    Old entries are plain strings or unstructured dicts without this
    field, which is straightforward to distinguish."""
    return isinstance(entry, dict) and isinstance(entry.get("signal"), str)


# ── Helper constructors ──────────────────────────────────────────────
#
# One per signal. The helper's job is to encode the promotion policy
# (report flag + task_type + severity) so emit-site code doesn't have
# to think about it. If you're migrating an emit site, pick the
# matching helper; if no helper exists, add one here rather than
# hand-building the envelope.


def make_note(
    correlation_id: str,
    note: str,
    *,
    round: int | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """Generic wrapper for emits that haven't been migrated to a
    specific signal yet. Preserves the original string verbatim in
    ``note`` so back-compat is perfect. Not promoted."""
    return EmitEnvelope(
        signal="note",
        correlation_id=correlation_id,
        note=note,
        round=round,
        thread_id=thread_id,
        user_id=user_id,
    )


def make_retrieval_trace(
    correlation_id: str,
    *,
    search_id: str,
    query: str,
    mode: str,
    k: int,
    telemetry: dict[str, Any],
    round: int | None = None,
    thread_id: str | None = None,
) -> EmitEnvelope:
    """Retrieval trace emitted by the search_corpus skill (2026-04-28).

    Carries the rag service's ``RetrievalTracePayload`` (timing per
    stage, arm hit counts, top-N chunks with their reranker signal
    breakdown). Surfaces in the chat thinking_log under a "Retrieval"
    panel — analogous to how llm_calls and qa_score blocks render in
    the current technical-mode UI.

    Diagnostic-only: ``report_to_task_manager=False``. Same tier as
    ``critic_approved`` — useful for debugging retrieval quality but
    not actionable as a top-level dashboard event.

    The ``data`` dict mirrors the rag agent's spec verbatim so the FE
    panel can render top_chunks[].signals (per-chunk reranker
    contributions) directly without reshaping.
    """
    # Rag refined the spec 2026-04-28: top-level total_ms +
    # arm_hits.{bm25,vector}; older draft used nested timing.* +
    # arms.{bm25_hits,vec_hits}. Read both so the helper works
    # against whichever rev is live.
    t = telemetry or {}
    arm_hits = t.get("arm_hits") or {}
    arms_legacy = t.get("arms") or {}
    timing_legacy = t.get("timing") or {}
    bm25 = int(arm_hits.get("bm25") or arms_legacy.get("bm25_hits") or 0)
    vec = int(arm_hits.get("vector") or arms_legacy.get("vec_hits") or 0)
    returned = int(
        arms_legacy.get("returned")
        or len(t.get("chunks") or [])
        or 0
    )
    total_ms = int((t.get("total_ms") or timing_legacy.get("total_ms") or 0) + 0.5)
    note = (
        f"◌ corpus search: {returned} chunk{'s' if returned != 1 else ''} · "
        f"{total_ms}ms · BM25={bm25} vec={vec}"
    )
    step_id = f"round_{round}.retrieval" if round is not None else "retrieval"
    return EmitEnvelope(
        signal="retrieval_trace",
        correlation_id=correlation_id,
        step_id=step_id,
        data={
            "search_id": search_id,
            "query": (query or "")[:500],
            "mode": mode,
            "k": k,
            **(telemetry or {}),
        },
        note=note,
        round=round,
        thread_id=thread_id,
        report_to_task_manager=False,
    )


# ── Critic signals (migrated in commit 1) ──────────────────────────


def make_critic_audit_started(
    correlation_id: str,
    round: int,
    *,
    draft_length: int,
    sources_count: int,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """The critic is about to audit a completion draft. Not promoted
    — internal step that leads to flagged/approved outcome."""
    return EmitEnvelope(
        signal="critic_audit_started",
        correlation_id=correlation_id,
        step_id=f"round_{round}.critic_audit",
        round=round,
        note="◌ Critic auditing draft against sources…",
        data={
            "draft_length": draft_length,
            "sources_count": sources_count,
        },
        thread_id=thread_id,
        user_id=user_id,
    )


def make_critic_flagged(
    correlation_id: str,
    round: int,
    *,
    total_issues: int,
    high_severity: int,
    flagged_claims: list[str],
    rounds_remaining: int,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """The critic rejected the draft — at least one claim isn't grounded
    in any retrieved source. Promoted as ``insight`` (med severity) so
    the chat PM can track hallucination catch-rate over time."""
    return EmitEnvelope(
        signal="critic_flagged",
        correlation_id=correlation_id,
        step_id=f"round_{round}.critic_flagged",
        round=round,
        note=f"⚠ Critic flagged {high_severity} ungrounded claim(s); revising in next round.",
        data={
            "total_issues": total_issues,
            "high_severity": high_severity,
            "rounds_remaining": rounds_remaining,
            # Cap claim preview at 5 × 200 chars so a single envelope
            # stays compact. Full claim list is still in
            # thinking_log-embedded tool_result from critic but that's
            # not promoted.
            "flagged_claims_preview": [c[:200] for c in flagged_claims[:5]],
        },
        thread_id=thread_id,
        user_id=user_id,
        report_to_task_manager=True,
        task_type="insight",
        task_severity="med",
    )


def make_critic_approved(
    correlation_id: str,
    round: int,
    *,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """The critic approved on first audit (no retry). Common case —
    not promoted. Sprint A.2's writer skips these to avoid
    task-manager noise."""
    return EmitEnvelope(
        signal="critic_approved",
        correlation_id=correlation_id,
        step_id=f"round_{round}.critic_approved",
        round=round,
        note="✓ Critic approved.",
        thread_id=thread_id,
        user_id=user_id,
    )


def make_critic_approved_after_retry(
    correlation_id: str,
    round: int,
    *,
    retry_count: int,
    issues_resolved: list[str],
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """The critic approved on a retry (after flagging previously) —
    evidence of system self-correction. Promoted as ``insight`` (low
    severity) for chat PM analytics: 'how often does the loop
    successfully recover from a hallucination?'
    """
    return EmitEnvelope(
        signal="critic_approved_after_retry",
        correlation_id=correlation_id,
        step_id=f"round_{round}.critic_approved_after_retry",
        round=round,
        note=f"✓ Critic approved after {retry_count} revision round(s).",
        data={
            "retry_count": retry_count,
            "issues_resolved_preview": [i[:200] for i in issues_resolved[:5]],
        },
        thread_id=thread_id,
        user_id=user_id,
        report_to_task_manager=True,
        task_type="insight",
        task_severity="low",
    )


def make_rounds_exhausted_with_warning(
    correlation_id: str,
    round: int,
    *,
    unresolved_claims: list[str],
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """ReAct rounds exhausted while the critic still had unresolved
    high-severity flags. The answer ships WITH a groundedness warning
    appended. Promoted as ``blocker`` (high severity) — the user should
    verify the flagged claims before acting. This is the hard-fail
    signal the chat PM watches for."""
    return EmitEnvelope(
        signal="rounds_exhausted_with_warning",
        correlation_id=correlation_id,
        step_id=f"round_{round}.rounds_exhausted",
        round=round,
        note=(
            f"⚠ Critic flagged {len(unresolved_claims)} unresolved claim(s); "
            "shipping with warning (rounds exhausted)."
        ),
        data={
            "unresolved_claims_count": len(unresolved_claims),
            "unresolved_claims_preview": [c[:200] for c in unresolved_claims[:5]],
        },
        thread_id=thread_id,
        user_id=user_id,
        report_to_task_manager=True,
        task_type="blocker",
        task_severity="high",
    )


# ── Tool / pipeline signals (commit 3 — fan-out) ────────────────────


def make_tool_exhausted(
    correlation_id: str,
    round: int,
    *,
    tool: str,
    attempts: int,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """A tool has been tried N times (N ≥ _TOOL_EXHAUSTION_THRESHOLD)
    with no productive output — the retry guard blocks further uses
    of it for the rest of the turn. Promoted as ``insight`` (med) —
    analytics signal for RAG / corpus / tool quality tuning."""
    return EmitEnvelope(
        signal="tool_exhausted",
        correlation_id=correlation_id,
        step_id=f"round_{round}.tool_exhausted",
        round=round,
        note=f"⊘ {tool} exhausted ({attempts} failures, no new evidence) — pivoting to a different tool.",
        data={"tool": tool, "attempts_before_exhaustion": attempts},
        thread_id=thread_id,
        user_id=user_id,
        report_to_task_manager=True,
        task_type="insight",
        task_severity="med",
    )


def make_tool_failed(
    correlation_id: str,
    round: int,
    *,
    tool: str,
    error_code: str,
    error_message: str,
    retryable: bool,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """A tool call produced a typed error envelope. Promoted only
    when non-recoverable (retryable=False) — the retry path handles
    retryable errors without needing analytics. Promoted as
    ``failure`` (med) — feeds per-tool error-rate dashboards."""
    return EmitEnvelope(
        signal="tool_failed",
        correlation_id=correlation_id,
        step_id=f"round_{round}.tool_failed",
        round=round,
        note=f"⊘ {tool} failed ({error_code}): {error_message[:120]}",
        data={
            "tool": tool,
            "error_code": error_code,
            "error_message": error_message[:500],
            "retryable": retryable,
        },
        thread_id=thread_id,
        user_id=user_id,
        # Only non-recoverable failures promote. Rate-limit + timeout
        # are retryable and handled locally — too noisy to promote
        # each one; the retry guard surfaces them in aggregate via
        # tool_exhausted.
        report_to_task_manager=not retryable,
        task_type="failure" if not retryable else None,
        task_severity="med" if not retryable else None,
    )


def make_rate_limit_hit(
    correlation_id: str,
    round: int,
    *,
    tool: str,
    provider: str | None = None,
    retry_after_seconds: float | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """Upstream provider rate-limited us. Promoted as ``failure``
    (high) — this is a capacity/credit issue operators should see
    surface quickly (it's what caused the 2026-04-19 'Anthropic 400
    credits' class of failure)."""
    return EmitEnvelope(
        signal="rate_limit_hit",
        correlation_id=correlation_id,
        step_id=f"round_{round}.rate_limit_hit",
        round=round,
        note=f"⊘ Rate-limited by {provider or tool}" + (
            f"; retrying in {retry_after_seconds:.1f}s" if retry_after_seconds else ""
        ),
        data={
            "tool": tool,
            "provider": provider,
            "retry_after_seconds": retry_after_seconds,
        },
        thread_id=thread_id,
        user_id=user_id,
        report_to_task_manager=True,
        task_type="failure",
        task_severity="high",
    )


def make_guidance_mode_activated(
    correlation_id: str,
    round: int,
    *,
    rounds_remaining: int,
    tools_used_so_far: list[str],
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """The planner hit the 80% threshold and shifted from 'hunt for
    authoritative answer' to 'synthesize next-best guidance from
    what we have'. Promoted as ``insight`` (low) — frequency signals
    when the hunt phase needs tuning or when queries consistently
    need hedging."""
    return EmitEnvelope(
        signal="guidance_mode_activated",
        correlation_id=correlation_id,
        step_id=f"round_{round}.guidance_mode",
        round=round,
        note=f"◌ Guidance mode activated (round {round}, {rounds_remaining} rounds remaining)",
        data={
            "rounds_remaining": rounds_remaining,
            "tools_used_so_far": tools_used_so_far,
        },
        thread_id=thread_id,
        user_id=user_id,
        report_to_task_manager=True,
        task_type="insight",
        task_severity="low",
    )


def make_confidence_filter_dropped_all(
    correlation_id: str,
    round: int,
    *,
    query: str,
    chunks_retrieved: int,
    confidence_min: float,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """search_corpus retrieved N chunks but all fell below the
    confidence_min threshold — zero reach the planner. Promoted as
    ``insight`` (low) — the threshold-tuning signal that motivated
    the 0.5→0.3 lowering in 760f06f. Tracks whether further tuning
    is needed per query class."""
    return EmitEnvelope(
        signal="confidence_filter_dropped_all",
        correlation_id=correlation_id,
        step_id=f"round_{round}.confidence_filter",
        round=round,
        note=(
            f"◌ Confidence filter ({confidence_min}) dropped all "
            f"{chunks_retrieved} retrieved chunks — no corpus evidence reached the planner."
        ),
        data={
            "query_preview": query[:200],
            "chunks_retrieved": chunks_retrieved,
            "confidence_min": confidence_min,
        },
        thread_id=thread_id,
        user_id=user_id,
        report_to_task_manager=True,
        task_type="insight",
        task_severity="low",
    )


def make_turn_started(
    correlation_id: str,
    *,
    mode: str,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """A chat turn began. NOT promoted today — too common; the
    complement signal turn_completed carries outcome data. Kept as
    a separate helper in case operators want per-turn counting
    later; flip report_to_task_manager=True locally if needed."""
    return EmitEnvelope(
        signal="turn_started",
        correlation_id=correlation_id,
        step_id="turn_start",
        note=f"Turn started ({mode})",
        data={"mode": mode},
        thread_id=thread_id,
        user_id=user_id,
    )


def make_turn_completed(
    correlation_id: str,
    *,
    rounds_used: int,
    tools_used: list[str],
    final_signal: str,
    duration_ms: int,
    total_llm_tokens: int | None = None,
    total_cost_usd: float | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """A chat turn finished successfully. Promoted as ``info``
    (low) — the throughput + cost-per-turn + rounds-distribution
    dashboard foundation."""
    return EmitEnvelope(
        signal="turn_completed",
        correlation_id=correlation_id,
        step_id="turn_complete",
        note=f"✓ Turn completed in {rounds_used} round(s), {duration_ms}ms",
        data={
            "rounds_used": rounds_used,
            "tools_used": tools_used,
            "final_signal": final_signal,
            "duration_ms": duration_ms,
            "total_llm_tokens": total_llm_tokens,
            "total_cost_usd": total_cost_usd,
        },
        thread_id=thread_id,
        user_id=user_id,
        report_to_task_manager=True,
        task_type="info",
        task_severity="low",
    )


def make_turn_failed(
    correlation_id: str,
    *,
    error_class: str,
    stage: str,
    error_message: str,
    last_tool: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """A chat turn failed — the orchestrator caught an exception
    that prevented completion. Promoted as ``failure`` (high) —
    top-level failure rate dashboard."""
    return EmitEnvelope(
        signal="turn_failed",
        correlation_id=correlation_id,
        step_id="turn_failed",
        note=f"✗ Turn failed at {stage}: {error_message[:120]}",
        data={
            "error_class": error_class,
            "stage": stage,
            "error_message": error_message[:500],
            "last_tool": last_tool,
        },
        thread_id=thread_id,
        user_id=user_id,
        report_to_task_manager=True,
        task_type="failure",
        task_severity="high",
    )


# ── Cache-assist signals (2026-04-23) ──────────────────────────────
#
# Four signals tracking the cached_answer_lookup skill lifecycle per
# turn. All four are chat-side-only (not promoted to task-manager) so
# they stay in thinking_log for debugging without cluttering
# dashboards. A future aggregation job can roll them up to daily
# cache-hit-rate metrics.


def make_cache_lookup_fired(
    correlation_id: str,
    *,
    mode: str,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """Cache lookup skill invoked at turn start.

    ``mode`` is one of: ``active`` (result shown to LLM), ``shadow``
    (result logged but not shown — A/B bypass bucket), ``off``
    (cache disabled for this turn — emitted only when debugging).
    """
    return EmitEnvelope(
        signal="cache_lookup_fired",
        correlation_id=correlation_id,
        step_id="cache.lookup",
        note=f"◌ Cache lookup ({mode})…",
        data={"mode": mode},
        thread_id=thread_id,
        user_id=user_id,
    )


def make_cache_candidates_returned(
    correlation_id: str,
    *,
    count: int,
    max_similarity: float | None,
    oldest_age_days: float | None,
    newest_age_days: float | None,
    reasons_filtered: dict,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """Cache lookup produced candidates. Emits even on zero-count so
    analytics can distinguish 'lookup ran, found nothing' from
    'lookup never ran'."""
    return EmitEnvelope(
        signal="cache_candidates_returned",
        correlation_id=correlation_id,
        step_id="cache.candidates",
        note=(
            f"✓ Cache returned {count} candidate(s)"
            + (f" · max sim {max_similarity:.2f}" if max_similarity is not None else "")
        ),
        data={
            "count": count,
            "max_similarity": max_similarity,
            "oldest_age_days": oldest_age_days,
            "newest_age_days": newest_age_days,
            "reasons_filtered": reasons_filtered,
        },
        thread_id=thread_id,
        user_id=user_id,
    )


def make_cache_influenced_decision(
    correlation_id: str,
    *,
    influence: str,
    cache_turn_id: str | None,
    similarity: float | None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """The final answer was materially influenced by a cached answer.

    ``influence`` one of: ``verbatim`` (cached answer text returned
    unchanged), ``partial`` (cache informed but answer re-synthesized),
    ``rejected`` (LLM saw cache and picked fresh retrieval instead).
    """
    return EmitEnvelope(
        signal="cache_influenced_decision",
        correlation_id=correlation_id,
        step_id="cache.influence",
        note=f"⚡ Cache influence: {influence}",
        data={
            "influence": influence,
            "cache_turn_id": cache_turn_id,
            "similarity": similarity,
        },
        thread_id=thread_id,
        user_id=user_id,
    )


def make_cache_rejected_by_llm(
    correlation_id: str,
    *,
    reason: str,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """LLM saw cache candidates but chose to invoke fresh retrieval.
    The orchestrator derives the reason from the candidate set when
    this fires — the LLM itself doesn't emit these."""
    return EmitEnvelope(
        signal="cache_rejected_by_llm",
        correlation_id=correlation_id,
        step_id="cache.rejected",
        note=f"⊘ Cache ignored: {reason}",
        data={"reason": reason},
        thread_id=thread_id,
        user_id=user_id,
    )


# ── Thinking-chain trace signals (2026-05-04) ─────────────────────────
#
# Five signals that narrate what the system is doing in plain English.
# All fire in react_loop.py during search tool dispatch, plus
# answer_quality fires from post_run_adjudication.py.
#
# Two-tier rendering contract:
#   ``note``  — plain English; shown to every user in the thinking trail
#   ``data``  — structured payload; rendered in technical mode only
#               (the FE checks ctx.chat_mode or a user preference flag)


def make_query_understood(
    correlation_id: str,
    *,
    query: str,
    intent_summary: str,
    round: int | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """Fires once per search invocation after the tool receives the query.

    ``intent_summary`` is a short phrase the pipeline derives from the
    query — e.g. "Molina mental-health billing codes in Florida". The
    note is what the user sees; data carries the raw query for
    technical-mode diff."""
    note = f"Got it — searching for: {intent_summary or query[:120]}"
    return EmitEnvelope(
        signal="query_understood",
        correlation_id=correlation_id,
        step_id=f"round_{round}.query_understood" if round is not None else "query_understood",
        note=note,
        data={
            "query": (query or "")[:400],
            "intent_summary": (intent_summary or "")[:200],
        },
        round=round,
        thread_id=thread_id,
        user_id=user_id,
    )


def make_strategy_selected(
    correlation_id: str,
    *,
    mode: str,
    reason: str | None = None,
    round: int | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """Fires immediately after make_query_understood.

    Translates the technical ``mode`` into plain English so users
    understand what kind of search is about to run.
    """
    _mode_notes = {
        "auto":      "Searching our authoritative materials",
        "corpus":    "Searching our authoritative materials",
        "precision": "Checking for exact policy text",
        "recall":    "Doing a broad scan across our materials",
        "fallback":  "Checking external sources",
    }
    base = _mode_notes.get(mode, "Searching")
    note = f"{base} {reason}…" if reason else f"{base}…"
    return EmitEnvelope(
        signal="strategy_selected",
        correlation_id=correlation_id,
        step_id=f"round_{round}.strategy_selected" if round is not None else "strategy_selected",
        note=note,
        data={
            "mode": mode,
            "reason": (reason or "")[:200],
        },
        round=round,
        thread_id=thread_id,
        user_id=user_id,
    )


def make_retrieval_complete(
    correlation_id: str,
    *,
    chunks_returned: int,
    tool: str,
    mode: str,
    top_score: float | None = None,
    round: int | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """Fires after a search round completes and we know how many chunks
    came back. ``top_score`` is the reranker score of the best chunk
    (0–1); rendered in technical mode, omitted in copilot."""
    if chunks_returned == 0:
        note = "Nothing found in this search — may try a different approach."
    elif chunks_returned == 1:
        note = "Found 1 relevant passage."
    else:
        note = f"Found {chunks_returned} relevant passages."
    if top_score is not None and chunks_returned > 0:
        note += f" (top relevance: {top_score:.0%})"
    return EmitEnvelope(
        signal="retrieval_complete",
        correlation_id=correlation_id,
        step_id=f"round_{round}.retrieval_complete" if round is not None else "retrieval_complete",
        note=note,
        data={
            "tool": tool,
            "mode": mode,
            "chunks_returned": chunks_returned,
            "top_score": top_score,
        },
        round=round,
        thread_id=thread_id,
        user_id=user_id,
    )


def make_fallback_triggered(
    correlation_id: str,
    *,
    from_tool: str,
    to_tool: str,
    reason: str,
    round: int | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """Fires when corpus search produces no usable evidence and the
    pipeline escalates to an external source or guidance mode."""
    _to_labels = {
        "google_search":  "checking external sources",
        "web_scrape":     "reading a web page",
        "guidance_mode":  "synthesizing from what we know",
    }
    to_label = _to_labels.get(to_tool, to_tool)
    note = f"Our materials didn't cover this — {to_label}."
    return EmitEnvelope(
        signal="fallback_triggered",
        correlation_id=correlation_id,
        step_id=f"round_{round}.fallback_triggered" if round is not None else "fallback_triggered",
        note=note,
        data={
            "from_tool": from_tool,
            "to_tool": to_tool,
            "reason": (reason or "")[:300],
        },
        round=round,
        thread_id=thread_id,
        user_id=user_id,
    )


def make_answer_quality(
    correlation_id: str,
    *,
    verdict: str,
    score: float,
    sub_scores: dict[str, Any],
    failure_stage: str | None = None,
    thread_id: str | None = None,
    user_id: str | None = None,
) -> EmitEnvelope:
    """Fires after the adjudicator completes. Promoted to task-manager
    as ``insight`` (med) — tracks answer quality over time and surfaces
    systemic coverage gaps.

    ``verdict`` is one of PASS / PARTIAL / FAIL.
    ``score`` is 0–1 (overall adjudicator score).
    ``sub_scores`` is the adjudicator's per-dimension breakdown dict.
    ``failure_stage`` (optional) names where the answer fell short.
    """
    v = (verdict or "FAIL").upper()
    if v == "PASS":
        note = "Answer verified — well grounded in our materials."
    elif v == "PARTIAL":
        note = "Partial answer — we found some but not all of what you need."
    else:
        note = "Couldn't fully answer this — may not be in our materials yet."

    return EmitEnvelope(
        signal="answer_quality",
        correlation_id=correlation_id,
        step_id="adjudicator.answer_quality",
        note=note,
        data={
            "verdict": v,
            "score": round(score, 3),
            "sub_scores": sub_scores or {},
            "failure_stage": failure_stage,
        },
        thread_id=thread_id,
        user_id=user_id,
        report_to_task_manager=True,
        task_type="insight",
        task_severity="med",
    )
