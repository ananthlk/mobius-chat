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
    "note",                           # generic fallback — plain-text emits migrated later
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
