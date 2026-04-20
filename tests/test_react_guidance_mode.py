"""ReAct guidance mode — the 80/20 hunt→synthesize split.

Why this file exists. The live 2026-04-19 validation turns on Sunshine
Health H0036 and Molina Florida behavioral health both burned all 6
rounds of searching and bailed with generic "I couldn't confirm"
messages — without ever giving the critic anything to audit. The
planner's completion threshold was too high: even when partial
evidence was in hand (Molina's own website scraped on round 4, the
Sunshine provider manual pages in context), the planner kept
searching instead of drafting a hedged answer.

The operator framed the right fix as an 80/20 split: give the
planner most of its rounds to find the authoritative answer, and the
last ~20% of rounds to shift into synthesis-from-partial-evidence
mode — produce a hedged "here's what I found, here's what you should
do next" answer rather than burn rounds and bail.

**Key property:** this is PURELY a prompt / round-control change.
The critic architecture is unchanged. Guidance-mode drafts are
audited by the same critic that audits normal-completion drafts —
and critically, the critic's rules (evidence-only, conservative,
honest-hedges-welcome) are exactly what's needed to keep
guidance-mode answers from degenerating into hallucinated
recommendations.

This file locks:

  1. **Threshold math.** ``guidance_mode_threshold`` returns the
     right round (ceil(0.8 * max)) for every mode, clamped so every
     mode has at least one guidance round.

  2. **Round classification.** ``is_guidance_round(i, max)`` returns
     True only for the guidance band. The 0-indexed iteration in is
     tested directly; callers that pass 1-indexed values get
     converted at the ``build_reasoning_context`` boundary.

  3. **Headline swap.** ``_react_round_headline`` renders the
     "Guidance — ..." label on guidance rounds so the user's thinking
     trail tells the truth about what phase the planner is in.

  4. **Instruction injection.** ``_react_guidance_instruction``
     returns the guidance prompt on guidance rounds and an empty
     string otherwise. The instruction text contains the three hard
     rules (no invented facts, no unsupported definitive assertions,
     no training-data extrapolation) so the critic has the right
     basis to reject bad guidance output.

  5. **``build_reasoning_context`` integration.** When
     ``max_iterations`` is passed, guidance rounds prepend the
     instruction. When it's omitted (legacy callers), behavior is
     identical to pre-guidance-mode — no change.
"""

from __future__ import annotations

import pytest

from app.pipeline.context import PipelineContext
from app.pipeline.react.prompts import (
    _react_guidance_instruction,
    _react_round_headline,
    build_reasoning_context,
    guidance_mode_threshold,
    is_guidance_round,
)


# ── Threshold math ──────────────────────────────────────────────────


class TestGuidanceThreshold:
    @pytest.mark.parametrize("max_it,expected", [
        (2, 2),   # quick: last round is guidance
        (3, 3),   # copilot: last round is guidance
        (4, 4),   # ceil(0.8 * 4) = 4
        (5, 4),   # ceil(0.8 * 5) = 4
        (6, 5),   # agentic: round 5 is first guidance, round 6 is last
        (10, 8),  # ceil(0.8 * 10) = 8
    ])
    def test_threshold_is_ceil_80_percent(self, max_it, expected):
        """Key invariant: threshold is the first round (1-indexed) at
        which guidance mode activates. At ceil(0.8 * max_it), which
        gives the planner most of the budget for hunting and at least
        one dedicated synthesis round (two on longer modes, so it can
        revise if the critic rejects)."""
        assert guidance_mode_threshold(max_it) == expected

    def test_threshold_is_never_below_two(self):
        """Even if max_it is 1 (shouldn't happen, but defensive),
        guidance must not fire on round 0 — the planner hasn't had
        a single tool call yet."""
        assert guidance_mode_threshold(1) >= 1
        # More realistically: small modes still give round 1 to hunt.
        assert guidance_mode_threshold(2) == 2


class TestRoundClassification:
    @pytest.mark.parametrize("mode_max,expected_guidance_rounds", [
        (2, {2}),           # quick: only round 2
        (3, {3}),           # copilot: only round 3
        (4, {4}),
        (6, {5, 6}),        # agentic: rounds 5 + 6 (one hunt buffer, one final)
    ])
    def test_guidance_rounds_per_mode(self, mode_max, expected_guidance_rounds):
        """Iterate every round, assert which ones are classified as
        guidance. Catches off-by-ones in the threshold comparison."""
        guidance_rounds_found = {
            i + 1 for i in range(mode_max)
            if is_guidance_round(i, mode_max)
        }
        assert guidance_rounds_found == expected_guidance_rounds

    def test_first_round_is_never_guidance(self):
        """Round 1 (iteration 0) is always hunt — the planner hasn't
        made a single tool call yet, so there's nothing to synthesize
        from. If this ever flipped, every turn would skip hunting."""
        for max_it in (2, 3, 4, 6, 10):
            assert is_guidance_round(0, max_it) is False, (
                f"Round 1 classified as guidance for max_it={max_it}"
            )

    def test_last_round_is_always_guidance(self):
        """Symmetric invariant: the final round of any mode is always
        guidance. Guarantees every mode has at least one synthesis
        attempt before rounds-exhaustion fires."""
        for max_it in (2, 3, 4, 6, 10):
            last = max_it - 1  # 0-indexed
            assert is_guidance_round(last, max_it) is True


# ── Headline swap ───────────────────────────────────────────────────


class TestRoundHeadline:
    def test_scoping_round_survives_guidance_check(self):
        """Round 0 is always 'Scoping' regardless of guidance math.
        Otherwise a 2-round mode would have round 0 classified as
        'Guidance' (wrong — no tool calls yet)."""
        for max_it in (2, 3, 6):
            assert "Scoping" in _react_round_headline(0, max_it)

    def test_guidance_round_gets_guidance_label(self):
        """Guidance-band rounds render a 'Guidance — ...' label. This
        is what the user SEES in the thinking trail — it tells them
        the planner has shifted from search to synthesis."""
        # agentic: round 5 (iteration 4) is first guidance round
        assert "Guidance" in _react_round_headline(4, 6)
        # agentic: round 6 is the final guidance round — distinct label
        assert "synthesize best next-step advice" in _react_round_headline(5, 6)

    def test_hunt_rounds_keep_existing_labels(self):
        """Pre-guidance behavior preserved for hunt rounds. Scoping /
        Grounding / Refinement / Extended labels still render."""
        # agentic, iteration 1: Grounding
        assert "Grounding" in _react_round_headline(1, 6)
        # agentic, iteration 2: Refinement
        assert "Refinement" in _react_round_headline(2, 6)
        # agentic, iteration 3: Extended
        assert "Extended" in _react_round_headline(3, 6)


# ── Guidance instruction content ───────────────────────────────────


class TestGuidanceInstruction:
    def test_empty_on_hunt_rounds(self):
        """Hunt-round reasoning context must not carry the guidance
        instruction — otherwise the planner starts pre-hedging from
        round 1, never hunts for the authoritative answer, and the
        80/20 split collapses. Empty string is the API contract."""
        assert _react_guidance_instruction(0, 6) == ""
        assert _react_guidance_instruction(1, 6) == ""
        assert _react_guidance_instruction(3, 6) == ""  # round 4 still hunt

    def test_present_on_guidance_rounds(self):
        instr = _react_guidance_instruction(4, 6)  # round 5 agentic
        assert "GUIDANCE MODE ACTIVATED" in instr
        assert "round 5 of 6" in instr
        assert "2 round(s) remain" in instr  # 6 - 4 = 2

    def test_hard_rules_present(self):
        """The critic audits guidance drafts. For the audit to produce
        useful rejections, the instruction must tell the planner the
        rules the critic enforces: no invented facts, no unsupported
        modal claims, no training-data extrapolation. If these drop
        from the prompt, guidance mode degenerates into a fabrication
        invitation."""
        instr = _react_guidance_instruction(4, 6)
        assert "Do NOT invent facts" in instr
        assert "Do NOT assert definitive requirements" in instr
        assert "Do NOT extrapolate from training-data" in instr
        # And the specific hallucination class that motivated this
        # work — fabricated phone numbers — is called out by example:
        assert "phone number" in instr.lower()

    def test_encourages_hedged_synthesis(self):
        """The instruction's POSITIVE guidance: produce a useful
        hedged answer. 'A useful hedged answer [...] is MUCH better
        than "I couldn't confirm"' is the line that pulls the planner
        out of the exhaustion-path default."""
        instr = _react_guidance_instruction(4, 6)
        assert "hedged" in instr.lower()
        assert "couldn" in instr.lower() or "next-step" in instr.lower()
        # Specifically encourages "is_complete: true":
        assert "is_complete" in instr

    def test_rounds_remaining_counter_is_accurate(self):
        """On the last round, ``rounds_remaining`` should be 1
        (this round). Catches off-by-one that would lie to the
        planner about whether it has room to revise."""
        # agentic, iteration 5 = round 6 of 6, last round
        instr = _react_guidance_instruction(5, 6)
        assert "1 round(s) remain" in instr
        # agentic, iteration 4 = round 5 of 6, one more after
        instr = _react_guidance_instruction(4, 6)
        assert "2 round(s) remain" in instr


# ── build_reasoning_context integration ─────────────────────────────


class TestBuildReasoningContextIntegration:
    def _ctx(self):
        ctx = PipelineContext(correlation_id="t", thread_id=None, message="q")
        ctx.effective_message = "q"
        ctx.last_turns = []
        return ctx

    def test_legacy_call_without_max_iterations_omits_guidance(self):
        """Backward compatibility: callers that pass only (ctx,
        tool_results, iteration) — the old signature — get identical
        pre-guidance-mode behavior. No guidance instruction leaks in."""
        out = build_reasoning_context(self._ctx(), [], 5)  # no max_iterations
        assert "GUIDANCE MODE ACTIVATED" not in out

    def test_hunt_round_with_max_iterations_still_no_guidance(self):
        """With max_iterations passed, hunt rounds still don't get the
        guidance instruction. Sanity check that the per-round gate
        actually fires."""
        out = build_reasoning_context(self._ctx(), [], 2, max_iterations=6)  # round 2 of 6
        assert "GUIDANCE MODE ACTIVATED" not in out

    def test_guidance_round_with_max_iterations_prepends_instruction(self):
        """The full loop's intended call: when round is in the
        guidance band and max_iterations is passed, the instruction
        prepends so the planner reads it FIRST before the rest of the
        context. Prepending matters because later context (tool
        results, conversation history) can be long — the model
        sometimes drops the middle. Guidance instruction at the top
        survives."""
        out = build_reasoning_context(
            self._ctx(), [], 5, max_iterations=6,  # round 5 agentic
        )
        assert "GUIDANCE MODE ACTIVATED" in out
        # Prepended — appears before the "User question:" line at bottom:
        guidance_idx = out.find("GUIDANCE MODE ACTIVATED")
        question_idx = out.find("User question:")
        assert guidance_idx < question_idx, (
            "Guidance instruction should be prepended, not appended"
        )

    def test_guidance_instruction_in_quick_mode_round_2(self):
        """Quick mode has only 2 rounds. Round 2 must get the
        guidance instruction — otherwise quick mode behaves identically
        to pre-guidance-mode and the 80/20 split is inactive for
        fast-path queries."""
        out = build_reasoning_context(
            self._ctx(), [], 2, max_iterations=2,
        )
        assert "GUIDANCE MODE ACTIVATED" in out


# ── Regression guard: existing build_reasoning_context tests still pass ──


class TestLegacyBehaviorPreserved:
    """The old build_reasoning_context tests (in test_react_loop.py)
    call with 3 positional args and assert specific phrases appear.
    That test file is quarantined out of the CI subset (known
    mobius_contracts failure), but the API contract still matters.
    Duplicate the key assertions here so the 3-arg call path is
    locked even if test_react_loop.py can't run."""

    def _ctx_with_active(self):
        ctx = PipelineContext(
            correlation_id="c",
            thread_id="t",
            message="What is Sunshine Health's PA process?",
        )
        ctx.merged_state = {"active": {"payer": "Sunshine Health", "jurisdiction": "Florida"}}
        ctx.effective_message = ctx.message
        ctx.last_turns = []
        return ctx

    def test_three_arg_call_still_works(self):
        """The pre-guidance call signature (ctx, tool_results,
        iteration) must not break. Ensures old callers — tests,
        scripts, any external integrators — keep working."""
        out = build_reasoning_context(self._ctx_with_active(), [], 1)
        assert "Sunshine Health" in out or "Florida" in out
        assert "What is Sunshine Health" in out

    def test_tool_results_still_included(self):
        """Verify the tool_results -> context rendering path wasn't
        damaged by prepending the guidance instruction."""
        tool_results = [
            {"tool": "search_corpus", "success": False,
             "result": "No relevant documents found."},
        ]
        out = build_reasoning_context(
            self._ctx_with_active(), tool_results, 2, max_iterations=6,
        )
        assert "search_corpus" in out
        assert "No relevant" in out
