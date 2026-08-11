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
