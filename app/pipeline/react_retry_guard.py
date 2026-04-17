"""Smart-retry guard for the ReAct loop (Phase 0.7).

Prevents the pathology observed in production where the bandit or the LLM
decision picks the *same* tool with the *same* inputs across rounds after it
already failed, burning LLM calls on a known-dead path.

Rules
-----
1. **Same tool + same inputs + no new evidence → skip.** A "failed attempt"
   is recorded when a tool execution either raises, returns ``success=False``,
   or returns a result attached to an :class:`ErrorEnvelope`. Before the next
   tool execution, if the (tool, inputs_signature) pair matches a prior
   failure, we check whether *new* tool results have been recorded since that
   failure. If not, we refuse to re-run it and tell the LLM to pick
   differently.

2. **Fail-fast at loop end.** If every round produced a failed attempt and
   no successful tool result was recorded, the loop short-circuits the final
   "escalate honestly" path and emits a typed refusal envelope instead.

The state lives on ``ReactRetryGuard`` so the ReAct loop can call it
idempotently and test seams stay tight.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FailedAttempt:
    """Record of a failed tool attempt inside one ReAct turn."""

    tool: str
    inputs_sig: str
    error_code: str | None
    round: int
    # Number of tool_results entries recorded BEFORE this failure. Used to
    # detect whether *new* evidence has accumulated since the failure.
    results_before: int


def inputs_signature(inputs: dict[str, Any] | None) -> str:
    """Stable, normalized signature for a tool's inputs dict.

    - Keys sorted alphabetically.
    - String values lowercased and trimmed.
    - None-valued keys dropped.
    - Non-string scalars serialized as JSON.

    Returns a short hex digest; two "same-enough" input dicts produce the
    same signature regardless of insertion order or trivial whitespace.
    """
    if not inputs:
        return "empty"
    norm: dict[str, Any] = {}
    for k in sorted(inputs.keys()):
        v = inputs[k]
        if v is None:
            continue
        if isinstance(v, str):
            norm[k] = v.strip().lower()
        else:
            try:
                norm[k] = json.loads(json.dumps(v, sort_keys=True, default=str))
            except (TypeError, ValueError):
                norm[k] = str(v)
    payload = json.dumps(norm, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class ReactRetryGuard:
    """Track failed attempts and advise the ReAct loop on retry / fail-fast."""

    failed_attempts: list[FailedAttempt] = field(default_factory=list)
    successful_attempts: int = 0

    # ── Public API ──────────────────────────────────────────────────────────

    def record_result(
        self,
        *,
        tool: str,
        inputs: dict[str, Any] | None,
        result: dict[str, Any],
        round: int,
        results_count_before: int,
    ) -> None:
        """Append a failed or successful attempt from a completed tool run."""
        if self._is_failure(result):
            self.failed_attempts.append(
                FailedAttempt(
                    tool=tool,
                    inputs_sig=inputs_signature(inputs),
                    error_code=self._error_code(result),
                    round=round,
                    results_before=results_count_before,
                )
            )
        else:
            self.successful_attempts += 1

    def should_block(
        self,
        *,
        tool: str,
        inputs: dict[str, Any] | None,
        current_results_count: int,
    ) -> FailedAttempt | None:
        """Return the FailedAttempt to cite if the planner is repeating a dead call.

        A call is blocked when:
        - A prior failed attempt matches the same (tool, inputs_sig), AND
        - No new tool_results have been recorded since that failure, i.e.
          ``current_results_count <= failed_attempt.results_before``.

        "New results since failure" includes successful tool runs of *other*
        tools — the planner may have learned something useful in the interim
        and genuinely want to retry with that context.
        """
        sig = inputs_signature(inputs)
        for fa in self.failed_attempts:
            if fa.tool == tool and fa.inputs_sig == sig:
                if current_results_count <= fa.results_before:
                    return fa
        return None

    def all_rounds_failed(self, rounds_completed: int) -> bool:
        """True when every round that ran produced a failure and nothing succeeded.

        Used by the ReAct loop to decide whether to emit a clean typed refusal
        instead of the legacy "I wasn't able to find a verified answer…" string.
        """
        if rounds_completed <= 0:
            return False
        return (
            self.successful_attempts == 0
            and len(self.failed_attempts) >= rounds_completed
        )

    def failure_hint_for_prompt(self) -> str:
        """Human-readable list of already-failed attempts for the reasoning prompt.

        The ReAct loop injects this into ``build_reasoning_context`` so the
        LLM sees which (tool, inputs) it should NOT pick again unless it has
        new context to work with.
        """
        if not self.failed_attempts:
            return ""
        lines = ["Already-failed attempts (do not repeat unless new evidence warrants it):"]
        for fa in self.failed_attempts:
            code = fa.error_code or "error"
            lines.append(f"  - round {fa.round}: {fa.tool} [{code}]")
        return "\n".join(lines)

    # ── Helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _is_failure(result: dict[str, Any]) -> bool:
        if result.get("success") is False:
            return True
        if result.get("error") is not None:
            return True
        return False

    @staticmethod
    def _error_code(result: dict[str, Any]) -> str | None:
        err = result.get("error")
        if isinstance(err, dict):
            code = err.get("error_code")
            if isinstance(code, str):
                return code
        if result.get("success") is False:
            return "tool_error"
        return None
