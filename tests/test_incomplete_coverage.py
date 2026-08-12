"""Task #84 (2026-08-11, Chat Master): incomplete_coverage exit +
"Continue gathering" signal.

react self-reports unfinished_reason="incomplete_coverage" when it found
real partial progress on a multi-item question (e.g. 2 of 4 payors) before
exhausting its round budget -- distinct from no_path_forward (nothing
panned out) and need_more_time (suggests a different mode entirely, not
"more of the same would finish the job"). Backend-computed
(ctx.react_unfinished_reason/ctx.react_unfinished_summary), never
LLM-produced by the integrator itself -- same additive pattern as
suggest_escalate/cta_confirm_authoritative, injected into the card in
run_integrate and allowlisted so it survives persistence.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from app.communication.assistant_envelope import build_assistant_envelope_v1
from app.pipeline.context import PipelineContext  # noqa: F401 -- import order avoids a circular import
from app.planner.schemas import Plan, SubQuestion
from app.stages.integrate import run_integrate


def _make_ctx(**extra):
    plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])
    ctx = PipelineContext(
        correlation_id="test-incomplete-coverage-cid",
        thread_id="test-thread",
        message="What are the timely filing deadlines for Sunshine, Molina, and Humana?",
        plan=plan,
        answers=["Some answer"],
        sources=[],
        usages=[],
        retrieval_signals=[],
    )
    for k, v in extra.items():
        setattr(ctx, k, v)
    return ctx


class TestRunIntegrateInjection:
    def test_incomplete_coverage_injected_into_card(self):
        ctx = _make_ctx(
            react_unfinished_reason="incomplete_coverage",
            react_unfinished_summary="Found deadlines for Sunshine and Molina; ran out of rounds before Humana.",
        )
        card = {"mode": "FACTUAL", "direct_answer": "Partial answer.", "sections": []}
        with patch("app.stages.integrate.format_response") as mock_format:
            mock_format.return_value = (json.dumps(card), None)
            run_integrate(ctx)

        payload = json.loads(ctx.response_payload["message"])
        assert payload.get("incomplete_coverage") is True
        assert payload.get("incomplete_coverage_summary") == (
            "Found deadlines for Sunshine and Molina; ran out of rounds before Humana."
        )

    def test_no_path_forward_does_not_set_incomplete_coverage(self):
        """Regression guard: the two reasons are distinct signals -- a
        genuinely-stalled turn must not also claim partial coverage."""
        ctx = _make_ctx(react_unfinished_reason="no_path_forward")
        card = {"mode": "FACTUAL", "direct_answer": "Stalled.", "sections": []}
        with patch("app.stages.integrate.format_response") as mock_format:
            mock_format.return_value = (json.dumps(card), None)
            run_integrate(ctx)

        payload = json.loads(ctx.response_payload["message"])
        assert "incomplete_coverage" not in payload

    def test_no_unfinished_reason_does_not_set_incomplete_coverage(self):
        ctx = _make_ctx()
        card = {"mode": "FACTUAL", "direct_answer": "Clean answer.", "sections": []}
        with patch("app.stages.integrate.format_response") as mock_format:
            mock_format.return_value = (json.dumps(card), None)
            run_integrate(ctx)

        payload = json.loads(ctx.response_payload["message"])
        assert "incomplete_coverage" not in payload

    def test_summary_omitted_when_absent(self):
        ctx = _make_ctx(react_unfinished_reason="incomplete_coverage")
        card = {"mode": "FACTUAL", "direct_answer": "Partial answer.", "sections": []}
        with patch("app.stages.integrate.format_response") as mock_format:
            mock_format.return_value = (json.dumps(card), None)
            run_integrate(ctx)

        payload = json.loads(ctx.response_payload["message"])
        assert payload.get("incomplete_coverage") is True
        assert "incomplete_coverage_summary" not in payload


def _minimal_kwargs(**overrides):
    base = dict(
        ui_blocks_raw=None,
        tool_fired="search_corpus",
        response_sources=[],
        next_steps=[],
        next_questions_for_user=[],
        roster_report_final_md=None,
        has_roster_pdf=False,
    )
    base.update(overrides)
    return base


class TestEnvelopeCalloutAndChip:
    def test_callout_and_chip_emitted(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Partial answer.", "sections": [],
                "incomplete_coverage": True,
                "incomplete_coverage_summary": "Found Sunshine and Molina; ran out of rounds before Humana.",
            },
            **_minimal_kwargs(),
        )
        callouts = [b for b in env["blocks"] if b["type"] == "callout"]
        assert any("Partial answer" in c["body"] for c in callouts)
        assert any("Sunshine and Molina" in c["body"] for c in callouts)

        chips_block = next(b for b in env["blocks"] if b["type"] == "action_chips")
        continue_chip = next(c for c in chips_block["chips"] if c["type"] == "continue_search")
        assert continue_chip["label"] == "Continue gathering"

    def test_no_callout_or_chip_when_absent(self):
        env = build_assistant_envelope_v1(
            answer_card={"mode": "FACTUAL", "direct_answer": "Clean answer.", "sections": []},
            **_minimal_kwargs(),
        )
        assert not any(b["type"] == "callout" for b in env["blocks"])
        assert not any(b["type"] == "action_chips" for b in env["blocks"])

    def test_continue_search_chip_merges_with_external_link_chips(self):
        """Both chip types land in the SAME action_chips block, not two
        separate blocks -- order preserved, external_link chips first."""
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Partial answer.", "sections": [],
                "incomplete_coverage": True,
                "suggested_actions": [
                    {"type": "external_link", "label": "Open Appeals Agent", "url": "https://example.com"},
                ],
            },
            **_minimal_kwargs(),
        )
        chip_blocks = [b for b in env["blocks"] if b["type"] == "action_chips"]
        assert len(chip_blocks) == 1
        chips = chip_blocks[0]["chips"]
        assert [c["type"] for c in chips] == ["external_link", "continue_search"]

    def test_callout_without_summary_still_emits_generic_body(self):
        env = build_assistant_envelope_v1(
            answer_card={
                "mode": "FACTUAL", "direct_answer": "Partial answer.", "sections": [],
                "incomplete_coverage": True,
            },
            **_minimal_kwargs(),
        )
        callout = next(b for b in env["blocks"] if b["type"] == "callout")
        assert "Partial answer" in callout["body"]


# ── Detection follow-up (2026-08-12, Chat Master, live finding cid=843e0dd0) ──
# incomplete_coverage was only ever checked on the is_complete=false
# exhausted-iterations fallback -- but the model completing NORMALLY
# (is_complete=true) on the final round with an acknowledged-but-unresolved
# gap (e.g. "found Sunshine and Aetna, couldn't reach Molina within the
# given rounds") is the MORE common case and was completely invisible to
# this signal. Fixed by deriving it from gaps_open non-empty on the final
# round, independent of is_complete, using the same reasoning-ledger data
# already flowing through evidence_review every round.

class TestGracefulCompletionWithGapsDetection:
    def _make_ctx(self) -> PipelineContext:
        ctx = PipelineContext(
            correlation_id="c-graceful-gap", thread_id=None,
            message="What are the timely filing deadlines for Sunshine, Aetna, and Molina?",
        )
        ctx.effective_message = ctx.message
        ctx.merged_state = {}
        ctx.last_turns = []
        ctx.chat_mode = "quick"  # REACT_MAX_ROUNDS_QUICK = 2 -- round 2 is the final round
        return ctx

    def test_is_complete_true_with_unresolved_gaps_sets_incomplete_coverage(self):
        """The exact live scenario: round 2 (final) answers normally with
        is_complete=true, but evidence_review.gaps_open still lists an
        unreached payor. Must set ctx.react_unfinished_reason, not stay
        silent the way it did on the real turn."""
        from app.pipeline.react_loop import run_react

        ctx = self._make_ctx()
        call_count = 0

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return '{"thought": "Search Sunshine first.", "tool": "search_corpus", "inputs": {"query": "Sunshine timely filing"}, "is_complete": false}'
            return (
                '{"thought": "Found Sunshine, ran out of rounds for Molina.", '
                '"tool": null, "is_complete": true, '
                '"answer": "Sunshine: 180 days.", '
                '"evidence_review": {"running_answer": "Sunshine covered.", '
                '"gaps_closed": [], "gaps_open": ["timely filing deadline for Molina"]}}'
            )

        def fake_execute(tool, inputs, ctx, round_num, emit_fn, tool_emitter, skip_retry=False, open_gaps=None):
            return {
                "tool": "search_corpus", "success": True,
                "result": "Sunshine: 180 days.", "signal": "corpus_only",
                "sources": [{"document_name": "Manual", "index": 1, "text": "180 days"}],
                "usage": None,
            }

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", side_effect=fake_execute):
            run_react(ctx, emitter=None)

        assert ctx.react_unfinished_reason == "incomplete_coverage"
        assert "Molina" in (ctx.react_unfinished_summary or "")
        # The turn still completes normally with the partial answer --
        # this signal is additive metadata, not a failure/retry path.
        assert "Sunshine" in ctx.final_message

    def test_is_complete_true_with_no_gaps_does_not_set_signal(self):
        """Regression guard: a genuinely complete answer (no unresolved
        gaps) on the final round must not be flagged as partial."""
        from app.pipeline.react_loop import run_react

        ctx = self._make_ctx()
        call_count = 0

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return '{"thought": "Search first.", "tool": "search_corpus", "inputs": {"query": "timely filing"}, "is_complete": false}'
            return (
                '{"thought": "Fully answered.", "tool": null, "is_complete": true, '
                '"answer": "Timely filing is 180 days.", '
                '"evidence_review": {"running_answer": "Fully covered.", "gaps_closed": [], "gaps_open": []}}'
            )

        def fake_execute(tool, inputs, ctx, round_num, emit_fn, tool_emitter, skip_retry=False, open_gaps=None):
            return {
                "tool": "search_corpus", "success": True,
                "result": "Timely filing is 180 days.", "signal": "corpus_only",
                "sources": [{"document_name": "Manual", "index": 1, "text": "180 days"}],
                "usage": None,
            }

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", side_effect=fake_execute):
            run_react(ctx, emitter=None)

        assert getattr(ctx, "react_unfinished_reason", None) is None

    def test_gaps_open_on_non_final_round_does_not_set_signal(self):
        """Regression guard: gaps_open is normal mid-turn state (round 1
        of N almost always has open gaps) -- only the FINAL round's
        unresolved gaps mean anything. Uses copilot mode (3 rounds) and
        completes early on round 1 with is_complete=true + gaps_open set,
        which must NOT trigger the signal since round 1 != max_it."""
        from app.pipeline.react_loop import run_react

        ctx = PipelineContext(
            correlation_id="c-early-gaps", thread_id=None,
            message="What is the timely filing deadline?",
        )
        ctx.effective_message = ctx.message
        ctx.merged_state = {}
        ctx.last_turns = []
        ctx.chat_mode = "copilot"  # REACT_MAX_ROUNDS_COPILOT = 3

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            return (
                '{"thought": "Answering early with an open gap noted.", "tool": null, "is_complete": true, '
                '"answer": "Sunshine is 180 days.", '
                '"evidence_review": {"running_answer": "Partial.", "gaps_closed": [], "gaps_open": ["Aetna deadline"]}}'
            )

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            run_react(ctx, emitter=None)

        assert getattr(ctx, "react_unfinished_reason", None) is None
