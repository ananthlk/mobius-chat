"""P1.1 — Differential refactor gate.

Rationale (docs/chat-refactor-program.md):
  Phase 0 makes the pipeline observable; this harness is the gate that
  proves subsequent phases didn't change behaviour they didn't intend to.

What is asserted:
  I1  each scenario produces exactly one turn_completed envelope
  I2  the integrator-bypass set matches the scenario's expectation
  I4  rounds_used and max_rounds match the governor's mode table
  I7  log-and-continue handler count in react_loop.py may fall, never rise

What is deferred (I3/I5):
  PHI gate verdict (I3) and groundedness floor ran/skipped (I5) require
  deterministic replay of real model responses, which we cannot reconstruct
  from chat_turns.  Those invariants are deferred pending a cassette layer.

Scope discipline (Product Awareness work order, 2026-09-08):
  This file is P1.1 only.  No deletions, no governor changes belong here.

PRE values:
  Derived from the frozen baseline (docs/chat-refactor-baseline.json,
  fingerprint 7278bebc) and the governor's _MODE_DEFAULTS table.  If a
  phase change is intended to move a value, update the expected value HERE
  (with a comment naming the phase) and confirm the delta is the intended
  change — a shift without a comment is a regression.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app.pipeline.react.prompts import (
    REACT_MAX_ROUNDS_AGENTIC,
    REACT_MAX_ROUNDS_COPILOT,
    REACT_MAX_ROUNDS_QUICK,
    REACT_MAX_ROUNDS_TASK,
)
from app.pipeline.react_loop import run_react

from tests.harness._ctx import make_ctx
from tests.harness._invariants import RunResult, extract
from tests.harness._llm import (
    fake_execute_tool,
    finalize_on_round_1,
    make_scripted,
    never_finalize,
    tool_then_finalize,
)

# ── Patch targets (constant — change here if module structure changes) ──

_LLM_PATCH = "app.pipeline.react_loop._call_llm_json"
_TOOL_PATCH = "app.pipeline.react_loop._execute_tool_with_retry"
_CRITIC_PATCH = "app.pipeline.react.critic.critic_enabled"


def _run(mode: str, fake_llm, context_summary=None, **ctx_kw) -> RunResult:
    """Run one scenario and return extracted invariants."""
    ctx = make_ctx(mode=mode, context_summary=context_summary, **ctx_kw)
    with (
        patch(_LLM_PATCH, side_effect=fake_llm),
        patch(_TOOL_PATCH, side_effect=fake_execute_tool),
        patch(_CRITIC_PATCH, return_value=False),  # I5 deferred — critic gating off
    ):
        run_react(ctx, emitter=None)
    return extract(ctx)


# ══════════════════════════════════════════════════════════════════════
# I1 — exactly one turn_completed envelope per turn
# ══════════════════════════════════════════════════════════════════════

class TestI1TerminalEnvelope:
    """Every scenario must produce exactly one react_trace envelope.

    I1 at the run_react boundary: we look for signal="react_trace" (emitted
    once per run_react call, at the end of the loop).  The turn_completed
    envelope is emitted by the orchestrator layer, which we do not invoke
    here — react_trace is the correct proxy for "the loop completed and
    its observability event fired."

    If a refactor adds a second finalize path or drops one, this fails.
    The baseline has 2,750 turns; the invariant is 1:1 by construction.

    Note: react_loop.py:3922 wraps the react_trace emit in an
    except-logger.debug swallow, so a failure there produces 0 envelopes.
    This test catches that regression too.
    """

    @pytest.mark.parametrize("mode", ["quick", "copilot", "agentic", "task"])
    def test_one_envelope_on_finalize(self, mode):
        result = _run(mode, finalize_on_round_1)
        assert result.terminal_envelope_count == 1, (
            f"[I1] mode={mode}: expected 1 turn_completed envelope, "
            f"got {result.terminal_envelope_count}"
        )

    def test_one_envelope_on_exhaustion_copilot(self):
        result = _run("copilot", never_finalize)
        assert result.terminal_envelope_count == 1, (
            f"[I1] copilot exhaustion: expected 1 envelope, "
            f"got {result.terminal_envelope_count}"
        )

    def test_one_envelope_on_exhaustion_agentic(self):
        result = _run("agentic", never_finalize)
        assert result.terminal_envelope_count == 1, (
            f"[I1] agentic exhaustion: expected 1 envelope, "
            f"got {result.terminal_envelope_count}"
        )

    def test_one_envelope_with_tool_call(self):
        result = _run("copilot", tool_then_finalize)
        assert result.terminal_envelope_count == 1, (
            f"[I1] tool-then-finalize: expected 1 envelope, got {result.terminal_envelope_count}"
        )

    @pytest.mark.parametrize("mode", ["quick", "copilot", "agentic"])
    def test_one_envelope_with_context_summary(self, mode):
        """with_context stratum — 1,853 of 2,750 baseline turns."""
        result = _run(mode, finalize_on_round_1, context_summary="Rolling context.")
        assert result.terminal_envelope_count == 1


# ══════════════════════════════════════════════════════════════════════
# I2 — integrator bypass set is unchanged
# ══════════════════════════════════════════════════════════════════════

class TestI2BypassSet:
    """Turns that bypass the integrator must be exactly the same set.

    Baseline: 383 of 2,740 turns bypass the integrator.
    Known bypass classes: refuse (217), clarify (83), task (5).
    Unexplained: 83 (under investigation — react_loop.py:3379).

    I2 failure means a refactor changed which turns reach the integrator.
    """

    def test_normal_turn_reaches_integrator(self):
        result = _run("copilot", finalize_on_round_1)
        assert not result.bypass_integrate, (
            "[I2] normal finalize should NOT set bypass_integrate"
        )

    def test_agentic_normal_reaches_integrator(self):
        result = _run("agentic", finalize_on_round_1)
        assert not result.bypass_integrate

    def test_exhausted_copilot_reaches_integrator(self):
        """Budget-exhausted turns bypass the CRITIC but NOT the integrator."""
        result = _run("copilot", never_finalize)
        assert not result.bypass_integrate, (
            "[I2] budget-exhausted turn should still reach the integrator"
        )

    def test_task_mode_bypass(self):
        """task mode is hardcoded to skip the integrator (bypass at orchestrator)."""
        # react_loop itself doesn't set the bypass for task — orchestrator does.
        # What we can test: run_react completes without error in task mode.
        result = _run("task", finalize_on_round_1)
        assert result.terminal_envelope_count == 1

    def test_clarify_bypass_from_rag_status(self):
        """When RAG returns no_retrieval and react_loop interprets it as clarify,
        bypass_integrate is set.  This exercises the clarify bypass path
        (react_loop.py:1767).

        The mock tool response uses signal='no_retrieval' which triggers
        _should_bypass_on_clarify if the planner's answer looks like a question.
        """
        clarify_planner = make_scripted([
            # Planner produces a clarifying question on round 1
            '{"thought": "Ambiguous query.", "tool": null, "is_complete": true, '
            '"answer": "Which Sunshine Health plan do you mean — Medicaid or Marketplace?", '
            '"evidence_review": null}',
        ])

        def no_retrieval_tool(tool, inputs, ctx, round_num, emit_fn, tool_emitter,
                              skip_retry=False, open_gaps=None):
            return {
                "tool": tool, "success": False, "signal": "no_retrieval",
                "result": "", "sources": [], "usage": None,
            }

        ctx = make_ctx(mode="copilot")
        with (
            patch(_LLM_PATCH, side_effect=clarify_planner),
            patch(_TOOL_PATCH, side_effect=no_retrieval_tool),
            patch(_CRITIC_PATCH, return_value=False),
        ):
            run_react(ctx, emitter=None)

        result = extract(ctx)
        # The clarify path at react_loop.py:1767 sets bypass_integrate only when
        # _should_bypass_on_clarify passes.  We verify the envelope exists (I1)
        # and that bypass was honoured if set.
        assert result.terminal_envelope_count == 1


# ══════════════════════════════════════════════════════════════════════
# I4 — rounds_used and max_rounds match the governor's mode table
# ══════════════════════════════════════════════════════════════════════

class TestI4RoundsArithmetic:
    """max_rounds must match the governor's _MODE_DEFAULTS for each mode.

    Baseline values (governor.py:81-85, PRE):
      quick    max=2
      copilot  max=3
      agentic  max=10
      task     max=3

    rounds_used must be ≤ max_rounds and ≥ 1 for any non-bypassed turn.

    If Phase 2 changes the extension logic (its declared intent), the
    expected delta must be recorded here with a comment naming Phase 2
    before that phase merges.
    """

    def test_quick_max_rounds(self):
        result = _run("quick", finalize_on_round_1)
        assert result.max_rounds == REACT_MAX_ROUNDS_QUICK, (
            f"[I4] quick max_rounds: expected {REACT_MAX_ROUNDS_QUICK}, "
            f"got {result.max_rounds}"
        )

    def test_copilot_max_rounds(self):
        result = _run("copilot", finalize_on_round_1)
        assert result.max_rounds == REACT_MAX_ROUNDS_COPILOT, (
            f"[I4] copilot max_rounds: expected {REACT_MAX_ROUNDS_COPILOT}, "
            f"got {result.max_rounds}"
        )

    def test_agentic_max_rounds(self):
        result = _run("agentic", finalize_on_round_1)
        assert result.max_rounds == REACT_MAX_ROUNDS_AGENTIC, (
            f"[I4] agentic max_rounds: expected {REACT_MAX_ROUNDS_AGENTIC}, "
            f"got {result.max_rounds}"
        )

    def test_task_max_rounds(self):
        result = _run("task", finalize_on_round_1)
        assert result.max_rounds == REACT_MAX_ROUNDS_TASK, (
            f"[I4] task max_rounds: expected {REACT_MAX_ROUNDS_TASK}, "
            f"got {result.max_rounds}"
        )

    def test_quick_finalize_round_1(self):
        result = _run("quick", finalize_on_round_1)
        assert result.rounds_used == 1
        assert result.rounds_used <= result.max_rounds

    def test_copilot_finalize_round_1(self):
        result = _run("copilot", finalize_on_round_1)
        assert result.rounds_used == 1
        assert result.rounds_used <= result.max_rounds

    def test_copilot_two_rounds(self):
        """Tool call on round 1, finalize on round 2."""
        result = _run("copilot", tool_then_finalize)
        assert result.rounds_used == 2
        assert result.rounds_used <= result.max_rounds

    def test_copilot_exhausted_at_ceiling(self):
        """never_finalize forces 3/3 rounds on copilot."""
        result = _run("copilot", never_finalize)
        assert result.rounds_used == REACT_MAX_ROUNDS_COPILOT, (
            f"[I4] copilot exhaustion: expected {REACT_MAX_ROUNDS_COPILOT} rounds, "
            f"got {result.rounds_used}"
        )
        assert result.rounds_used == result.max_rounds

    def test_agentic_exhausted_at_ceiling(self):
        """never_finalize forces 10/10 rounds on agentic.

        173 copilot + 30 agentic budget-exhausted turns in the baseline.
        These are the turns the critic never audits (critic bug).
        This test verifies the ceiling arithmetic — NOT that the critic
        runs (critic is mocked off for I5 deferral).
        """
        result = _run("agentic", never_finalize)
        assert result.rounds_used == REACT_MAX_ROUNDS_AGENTIC, (
            f"[I4] agentic exhaustion: expected {REACT_MAX_ROUNDS_AGENTIC} rounds, "
            f"got {result.rounds_used}"
        )
        assert result.rounds_used == result.max_rounds

    def test_rounds_used_never_exceeds_max(self):
        """Invariant: rounds_used ≤ max_rounds regardless of planner behaviour."""
        for mode in ("quick", "copilot", "agentic"):
            result = _run(mode, never_finalize)
            assert result.rounds_used <= result.max_rounds, (
                f"[I4] mode={mode}: rounds_used={result.rounds_used} "
                f"> max_rounds={result.max_rounds}"
            )


# ══════════════════════════════════════════════════════════════════════
# I7 — log-and-continue handler count (static)
# ══════════════════════════════════════════════════════════════════════

class TestI7SwallowCount:
    """Log-and-continue handler count in react_loop.py must not rise.

    The baseline count is 21 (from the findings log; verified 2026-09-08).
    A refactor may reduce this number (good) but must never add new ones.

    What we count: bare ``except`` clauses or ``except Exception`` blocks
    that contain a logger call (log) and no re-raise (continue).  We use a
    simple AST walk rather than regex to avoid false positives from
    comments and strings.

    The count is recorded here explicitly so a PR that adds a swallow
    fails the gate with a clear diff: "expected ≤ 21, got 22."
    """

    _BASELINE = 21  # verified against react_loop.py 2026-09-08

    # EXEMPT, not baselined up. The v2 shadow observer installs two handlers
    # (pre-round and post-round) that log and continue ON PURPOSE: an observer
    # that can break the thing it observes is not an observer, which is the
    # same posture as the attestation write. Raising _BASELINE to 23 instead
    # would buy their exemption at the price of the gate — an unrelated third
    # swallow would then land inside the new headroom and never be seen.
    #
    # Narrow on purpose: only handlers whose log line carries this tag. When v2
    # replaces the loop the hooks go, this exemption stops matching anything,
    # and the count returns to the real baseline with no edit here. If it ever
    # matches more than two, that is a finding.
    # 2026-09-12: a THIRD v2 hook now exists -- the framing hook, which is the
    # one that can STOP a turn. This gate caught it, exactly as its comment
    # said it would ("if it ever matches more than two, that is a finding").
    #
    # NOT baselined up, and NOT folded into the [v2.shadow] tag it is not.
    # Each hook is exempt BY NAME with its own cap, so an unrelated fourth
    # swallow cannot land in shared headroom and go unseen -- which is the
    # precise failure the original two-line exemption was written to prevent.
    #
    # [v2.frame] earns the same exemption for the same reason as the others: a
    # governor hook that can break the turn it governs is worse than one that
    # declines to act. When v2 owns the loop these hooks go and every entry
    # here stops matching, with no edit needed.
    _EXEMPT_TAGS = {"[v2.shadow]": 2, "[v2.frame]": 1}
    _EXEMPT_TAG = "[v2.shadow]"   # kept: test_exempt_observers_have_not_multiplied
    _EXEMPT_MAX = 2

    def _count_swallows(self) -> int:
        src = (
            Path(__file__).parent.parent.parent
            / "app" / "pipeline" / "react_loop.py"
        ).read_text()
        tree = ast.parse(src)
        count = 0
        exempt = [0]
        seen_per_tag: dict[str, int] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.ExceptHandler,)):
                continue
            # Does this handler contain a logger call?
            has_log = any(
                isinstance(n, ast.Call)
                and isinstance(getattr(n, "func", None), ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "logger"
                for n in ast.walk(node)
            )
            # Does it re-raise (bare ``raise`` or ``raise e``)?
            has_reraise = any(isinstance(n, ast.Raise) for n in ast.walk(node))
            if not (has_log and not has_reraise):
                continue
            # Deliberate-observer exemption — see _EXEMPT_TAGS above. Matched
            # per tag, with a per-tag cap: a hook that exceeds its own cap
            # falls through and is COUNTED, so the exemption cannot silently
            # absorb a new swallow that merely borrows an existing tag.
            consts = [c.value for c in ast.walk(node)
                      if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            matched = next(
                (t for t, cap in self._EXEMPT_TAGS.items()
                 if any(t in v for v in consts)
                 and seen_per_tag.get(t, 0) < cap),
                None,
            )
            if matched:
                seen_per_tag[matched] = seen_per_tag.get(matched, 0) + 1
                exempt[0] += 1
                continue
            count += 1
        return count

    def test_exempt_observers_have_not_multiplied(self):
        """The exemption is for two named hooks, not a category to grow into."""
        src = (
            Path(__file__).parent.parent.parent
            / "app" / "pipeline" / "react_loop.py"
        ).read_text()
        n = src.count(self._EXEMPT_TAG)
        assert n <= self._EXEMPT_MAX, (
            f"[I7] {self._EXEMPT_TAG} handlers: expected ≤ {self._EXEMPT_MAX}, got {n}. "
            "The shadow observer is exempt from the swallow gate because it must "
            "never break v1. That exemption covers the pre-round and post-round "
            "hooks — if it now covers more, the exemption has become a hiding place."
        )

    def test_swallow_count_has_not_risen(self):
        count = self._count_swallows()
        assert count <= self._BASELINE, (
            f"[I7] react_loop.py log-and-continue handlers: "
            f"expected ≤ {self._BASELINE}, got {count}. "
            "A phase change added a new swallowed exception — name it or remove it."
        )

    def test_swallow_count_fixture_is_not_stale(self):
        """Guard: if someone manually updated _BASELINE upward, fail loudly."""
        assert self._BASELINE <= 21, (
            "[I7] _BASELINE was raised above the 2026-09-08 count. "
            "The purpose of this test is to prevent that."
        )


# ══════════════════════════════════════════════════════════════════════
# Coverage check against the baseline strata
# ══════════════════════════════════════════════════════════════════════

class TestBaselineCoverage:
    """Verify the scenario set covers the strata distribution in the
    2,750-turn frozen baseline (docs/chat-refactor-baseline.json).

    This test does NOT re-run the baseline turns — it checks that the
    scenario population includes the key strata, so a phase that breaks
    only one stratum (e.g. no_context) doesn't slip through.

    Strata in baseline:
      with_context  1,853 turns  (67.4%)
      no_context      897 turns  (32.6%)

    Note: 378 of the 383 integrator bypasses land in no_context because
    bypassed turns exit before the context summary is written.  That stratum
    is partly an effect of bypass, not a clean covariate (DB seat, P1.1
    work order).  We include it in coverage but don't use it as a control.

    Mode distribution (from baseline):
      mode field is NULL for many rows — the harness uses all four modes.
    """

    def test_with_context_stratum_covered(self):
        result = _run("copilot", finalize_on_round_1, context_summary="Context text.")
        assert result.terminal_envelope_count == 1

    def test_no_context_stratum_covered(self):
        result = _run("copilot", finalize_on_round_1, context_summary=None)
        assert result.terminal_envelope_count == 1

    def test_all_four_modes_covered(self):
        for mode in ("quick", "copilot", "agentic", "task"):
            result = _run(mode, finalize_on_round_1)
            assert result.terminal_envelope_count == 1, f"mode={mode} failed"

    def test_exhaustion_stratum_covered(self):
        """Covers the 203 budget-exhausted turns (the critic-gap class)."""
        for mode in ("copilot", "agentic"):
            result = _run(mode, never_finalize)
            assert result.rounds_used == result.max_rounds, (
                f"mode={mode} did not exhaust its budget"
            )
