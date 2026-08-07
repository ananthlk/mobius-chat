"""Tests for Task #68: backend-injected extras (react_draft, suggest_escalate)
surviving the "bleed" branches in run_integrate.

Root cause: when the integrator's direct_answer contains nested/bled JSON
(the model wrapped a resolutions/direct_answer blob inside its own
direct_answer string), run_integrate re-parses that nested JSON
(inner_parsed/res) and built display_message with extra_from=inner_parsed
(or extra_from=res) -- a FRESH, separate dict from the outer `parsed` where
react_draft/suggest_escalate were injected. Those backend-only fields were
never present in inner_parsed/res to begin with, so they silently vanished
from the persisted card on any turn that hit a bleed branch -- reproduced
live on a real quick-mode turn (draft_ready carried the correct hedge text;
ctx.react_draft was confirmed present and non-empty at the read point; the
persisted final_message still had no react_draft key).

Fix: extra_from={**parsed, **inner_parsed} (inner_parsed's own keys still
win on overlap -- it's the corrected/nested content -- but parsed's unique
backend-injected keys now survive)."""
from __future__ import annotations

import json
from unittest.mock import patch

from app.pipeline.context import PipelineContext  # noqa: F401 -- import order avoids a circular import
from app.planner.schemas import Plan, SubQuestion
from app.stages.integrate import run_integrate


def _make_ctx(**extra):
    plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])
    ctx = PipelineContext(
        correlation_id="test-bleed-cid",
        thread_id="test-thread",
        message="What is X?",
        plan=plan,
        answers=["Some answer"],
        sources=[],
        usages=[],
        retrieval_signals=[],
    )
    for k, v in extra.items():
        setattr(ctx, k, v)
    return ctx


def test_react_draft_survives_case1_full_answercard_bleed():
    """direct_answer contains a full nested AnswerCard (Case 1 branch)."""
    ctx = _make_ctx(react_draft="ReAct's hedge: limited sources available.")
    inner = {"mode": "FACTUAL", "direct_answer": "The real answer.", "sections": []}
    card = {
        "mode": "FACTUAL",
        "direct_answer": json.dumps(inner),
        "sections": [],
    }
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("react_draft") == "ReAct's hedge: limited sources available."
    assert payload.get("direct_answer") == "The real answer."


def test_react_draft_survives_case2_nested_resolution_dict_bleed():
    """direct_answer contains nested resolutions[].resolution as a dict (Case 2)."""
    ctx = _make_ctx(react_draft="ReAct's hedge text here.")
    inner = {
        "resolutions": [
            {"resolution": {"mode": "FACTUAL", "direct_answer": "Nested answer.", "sections": []}}
        ]
    }
    card = {
        "mode": "FACTUAL",
        "direct_answer": json.dumps(inner),
        "sections": [],
    }
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("react_draft") == "ReAct's hedge text here."
    assert payload.get("direct_answer") == "Nested answer."


def test_react_draft_survives_case2b_plain_text_resolution_bleed():
    """direct_answer contains nested resolutions[].resolution as plain text (Case 2b)."""
    ctx = _make_ctx(react_draft="Hedge text for plain-text resolution case.")
    inner = {"mode": "FACTUAL", "resolutions": [{"resolution": "Plain text answer."}]}
    card = {
        "mode": "FACTUAL",
        "direct_answer": json.dumps(inner),
        "sections": [],
    }
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("react_draft") == "Hedge text for plain-text resolution case."
    assert payload.get("direct_answer") == "Plain text answer."


def test_react_draft_survives_top_level_resolutions_bleed():
    """Top-level parsed has "resolutions" directly (not nested in direct_answer)."""
    ctx = _make_ctx(react_draft="Top-level resolutions hedge.")
    card = {
        "mode": "FACTUAL",
        "resolutions": [
            {"resolution": {"mode": "FACTUAL", "direct_answer": "Top-level nested answer.", "sections": []}}
        ],
    }
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("react_draft") == "Top-level resolutions hedge."
    assert payload.get("direct_answer") == "Top-level nested answer."


def test_suggest_escalate_survives_bleed_branch():
    """suggest_escalate (also backend-injected, same mechanism as react_draft)
    must survive the same bleed branches."""
    ctx = _make_ctx(react_draft="x", react_unfinished_reason="no_path_forward", chat_mode="quick")
    inner = {"mode": "FACTUAL", "direct_answer": "Nested answer.", "sections": []}
    card = {"mode": "FACTUAL", "direct_answer": json.dumps(inner), "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("suggest_escalate") is True


def test_inner_content_still_wins_over_outer_on_overlapping_keys():
    """The merge must not let the outer (bled) parsed's direct_answer/mode
    leak back in over the corrected inner content -- inner_parsed's own
    keys must still win on overlap."""
    ctx = _make_ctx(react_draft="hedge")
    inner = {"mode": "CANONICAL", "direct_answer": "Corrected nested answer.", "sections": []}
    card = {"mode": "FACTUAL", "direct_answer": json.dumps(inner), "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("direct_answer") == "Corrected nested answer."
    assert payload.get("mode") == "CANONICAL"


def test_normal_non_bleed_card_still_gets_react_draft():
    """Regression guard: the ordinary (non-bleed) path must keep working
    exactly as before this fix."""
    ctx = _make_ctx(react_draft="normal path hedge")
    card = {"mode": "FACTUAL", "direct_answer": "A normal, non-bled answer.", "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("react_draft") == "normal path hedge"


def test_react_draft_survives_total_json_parse_failure_stub():
    """A total parse failure (final_message isn't valid JSON at all) must
    still carry react_draft/suggest_escalate -- this stub is built OUTSIDE
    the if isinstance(parsed, dict) block entirely, so the normal injection
    never runs there."""
    ctx = _make_ctx(react_draft="Limited sources available for this query.")
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = ("not valid json at all { broken", None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("react_draft") == "Limited sources available for this query."


def test_react_draft_survives_answercard_validation_failure_stub():
    """A structurally-invalid AnswerCard (missing required keys) gets
    replaced wholesale by _recital_fallback_card() -- must still carry
    react_draft/suggest_escalate."""
    ctx = _make_ctx(react_draft="Hedge text for validation-failure case.")
    # Missing "sections" -- fails the AnswerCard validator in run_integrate.
    card = {"mode": "FACTUAL", "direct_answer": "x"}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("react_draft") == "Hedge text for validation-failure case."


def test_suggest_escalate_survives_total_parse_failure_stub():
    ctx = _make_ctx(react_draft="x", react_unfinished_reason="no_path_forward", chat_mode="quick")
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = ("not valid json { broken", None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("suggest_escalate") is True


def test_backend_extras_omitted_when_react_draft_absent():
    """Regression guard: no react_draft on ctx -> stub cards must not
    fabricate one."""
    ctx = _make_ctx()
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = ("not valid json { broken", None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert "react_draft" not in payload


def test_ctx_final_message_matches_response_payload_message():
    """Task #68's TRUE root cause: ctx.final_message was set (at the RECITAL
    post-process step) BEFORE react_draft/suggest_escalate injection, bleed-
    branch handling, and stub construction all ran on a SEPARATE local
    variable (display_message) that only ever reached
    ctx.response_payload["message"] (the live API response), never
    ctx.final_message (what orchestrator.py's main save_turn call site
    persists). Every fixup in this file was invisible to the DB column the
    whole time. This is the actual regression test for the bug -- the two
    must be identical after run_integrate returns."""
    ctx = _make_ctx(react_draft="Verifying persisted value matches served value.")
    card = {"mode": "FACTUAL", "direct_answer": "A normal answer.", "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    assert ctx.final_message == ctx.response_payload["message"]
    persisted = json.loads(ctx.final_message)
    assert persisted.get("react_draft") == "Verifying persisted value matches served value."


def test_ctx_final_message_matches_response_payload_on_bleed_branch():
    ctx = _make_ctx(react_draft="hedge for bleed sync check")
    inner = {"mode": "FACTUAL", "direct_answer": "Nested answer.", "sections": []}
    card = {"mode": "FACTUAL", "direct_answer": json.dumps(inner), "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    assert ctx.final_message == ctx.response_payload["message"]
    assert json.loads(ctx.final_message).get("react_draft") == "hedge for bleed sync check"


def test_ctx_final_message_matches_response_payload_on_stub_path():
    ctx = _make_ctx(react_draft="hedge for stub sync check")
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = ("not valid json { broken", None)
        run_integrate(ctx)

    assert ctx.final_message == ctx.response_payload["message"]
    assert json.loads(ctx.final_message).get("react_draft") == "hedge for stub sync check"
