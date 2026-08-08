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


# ── cta_confirm_authoritative persistence gap (2026-08-08) ─────────────────
# Found verifying Task #41(a)'s "confirm from authoritative sources" CTA
# live: draft_ready's SSE payload (built directly from append_draft_answer,
# not through _answer_card_json_for_client) correctly carried
# cta_confirm_authoritative=True, but the SAME turn's persisted chat_turns
# row had it silently dropped. Root cause: _ANSWER_CARD_ENVELOPE_KEYS is a
# positive-filter allowlist (same class of bug this file's react_draft/
# suggest_escalate tests already cover) and cta_confirm_authoritative was
# never added to it when Task #41(a) shipped -- the field was set on
# `parsed` (integrate.py ~line 1141) but _answer_card_json_for_client only
# copies keys present in the allowlist tuple.

def test_cta_confirm_authoritative_survives_normal_path():
    ctx = _make_ctx(cta_confirm_authoritative=True)
    card = {"mode": "FACTUAL", "direct_answer": "A normal, broad-net answer.", "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("cta_confirm_authoritative") is True


def test_cta_confirm_authoritative_survives_bleed_branch():
    ctx = _make_ctx(cta_confirm_authoritative=True)
    inner = {"mode": "FACTUAL", "direct_answer": "Nested answer.", "sections": []}
    card = {"mode": "FACTUAL", "direct_answer": json.dumps(inner), "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("cta_confirm_authoritative") is True


def test_cta_confirm_authoritative_survives_total_parse_failure_stub():
    ctx = _make_ctx(cta_confirm_authoritative=True)
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = ("not valid json { broken", None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("cta_confirm_authoritative") is True


def test_cta_confirm_authoritative_omitted_when_absent():
    """Regression guard: absent/False on ctx -> never fabricated on the card."""
    ctx = _make_ctx()
    card = {"mode": "FACTUAL", "direct_answer": "A normal answer.", "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert "cta_confirm_authoritative" not in payload


# ── reasoning_trace (2026-08-08, Chat FE stepped-progression view) ─────────
# Chat FE agent asked for the per-round react ledger (rd-1 -> rd-N running_
# answer) to render a collapsed-by-default progression in the chat bubble.
# ctx.react_trace_rounds already carries this (built 2026-08-07 for Task
# #58); the only gap was that nothing threaded it onto the client-facing
# card. Reuses _build_reasoning_ledger (already computed for the enricher's
# own prompt input) rather than exposing raw ctx.reasoning_trace, so the FE
# gets the same capped/flattened round/running_answer/learned/gaps_open
# shape and internal fields (composition_id, raw_result_ref, inputs) never
# reach the wire. Same three-layer coverage (normal/bleed/stub) as
# react_draft/cta_confirm_authoritative above, since it rides the identical
# mechanism.

_SAMPLE_TRACE_ROUNDS = [
    {"round": 1, "tool": "search_corpus", "enrichment": {
        "learned": "Found the Florida Medicaid page.",
        "running_answer": "Eligibility requires FL residency and income limits.",
        "gaps_closed": [], "gaps_open": ["exact income threshold"],
    }},
    {"round": 2, "tool": "search_corpus", "enrichment": {
        "learned": "Confirmed the FPL threshold.",
        "running_answer": "Eligibility requires FL residency, US citizenship, and income under 138% FPL.",
        "gaps_closed": ["exact income threshold"], "gaps_open": [],
    }},
]


def test_reasoning_trace_survives_normal_path():
    ctx = _make_ctx(react_trace_rounds=_SAMPLE_TRACE_ROUNDS)
    card = {"mode": "FACTUAL", "direct_answer": "A normal, non-bled answer.", "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    trace = payload.get("reasoning_trace")
    assert trace is not None
    assert [r["round"] for r in trace] == [1, 2]
    assert trace[0]["running_answer"] == "Eligibility requires FL residency and income limits."
    assert trace[1]["gaps_closed"] == ["exact income threshold"]
    # Internal-only fields must not leak onto the client card.
    assert "composition_id" not in trace[0]
    assert "raw_result_ref" not in trace[0]


def test_reasoning_trace_survives_bleed_branch():
    ctx = _make_ctx(react_trace_rounds=_SAMPLE_TRACE_ROUNDS)
    inner = {"mode": "FACTUAL", "direct_answer": "Nested answer.", "sections": []}
    card = {"mode": "FACTUAL", "direct_answer": json.dumps(inner), "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert len(payload.get("reasoning_trace") or []) == 2


def test_reasoning_trace_survives_total_parse_failure_stub():
    ctx = _make_ctx(react_trace_rounds=_SAMPLE_TRACE_ROUNDS)
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = ("not valid json { broken", None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert len(payload.get("reasoning_trace") or []) == 2


def test_reasoning_trace_omitted_when_absent():
    """Regression guard: no react_trace_rounds on ctx -> never fabricated."""
    ctx = _make_ctx()
    card = {"mode": "FACTUAL", "direct_answer": "A normal answer.", "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert "reasoning_trace" not in payload


def test_reasoning_trace_omitted_when_rounds_have_no_enrichment():
    """Rounds that never ran an evidence_review (pure tool-dispatch, no
    running_answer yet) are skipped by _build_reasoning_ledger, not padded
    -- an all-skipped list means the ledger is empty and the key is omitted
    entirely rather than shipping an empty array."""
    ctx = _make_ctx(react_trace_rounds=[{"round": 1, "tool": "search_corpus"}])
    card = {"mode": "FACTUAL", "direct_answer": "A normal answer.", "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert "reasoning_trace" not in payload


# ── reasoning_trace persistence (2026-08-08, Chat FE stepped-progression view) ─
# Same allowlist-injection pattern as react_draft/cta_confirm_authoritative.
# Built from ctx.react_trace_rounds via _build_reasoning_ledger (the SAME
# function that already builds the enricher's own reasoning_ledger prompt
# input) -- flat per-round entries {round, tool, learned, running_answer,
# gaps_closed, gaps_open}, NOT nested under an "enrichment" key. Rounds with
# no enrichment content are skipped, not padded.

_TRACE_ROUNDS = [
    {"round": 1, "tool": "search_corpus", "enrichment": {
        "learned": "Corpus mentions Sunshine Health filing windows.",
        "running_answer": "Filing deadline is likely 180 days.",
        "gaps_closed": [], "gaps_open": ["resubmission window"],
    }},
    {"round": 2, "tool": "search_corpus", "enrichment": {
        "learned": "Resubmission window is 90 days.",
        "running_answer": "Initial 180 days, resubmission 90 days.",
        "gaps_closed": ["resubmission window"], "gaps_open": [],
    }},
]


def test_reasoning_trace_survives_normal_path():
    ctx = _make_ctx(react_trace_rounds=_TRACE_ROUNDS)
    card = {"mode": "FACTUAL", "direct_answer": "A normal answer.", "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    trace = payload.get("reasoning_trace")
    assert trace is not None and len(trace) == 2
    assert trace[0]["round"] == 1
    assert trace[0]["running_answer"] == "Filing deadline is likely 180 days."
    assert "enrichment" not in trace[0], "flat shape, not nested under enrichment"
    assert trace[1]["gaps_closed"] == ["resubmission window"]


def test_reasoning_trace_survives_bleed_branch():
    ctx = _make_ctx(react_trace_rounds=_TRACE_ROUNDS)
    inner = {"mode": "FACTUAL", "direct_answer": "Nested answer.", "sections": []}
    card = {"mode": "FACTUAL", "direct_answer": json.dumps(inner), "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("reasoning_trace") is not None
    assert len(payload["reasoning_trace"]) == 2


def test_reasoning_trace_survives_total_parse_failure_stub():
    ctx = _make_ctx(react_trace_rounds=_TRACE_ROUNDS)
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = ("not valid json { broken", None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert payload.get("reasoning_trace") is not None
    assert len(payload["reasoning_trace"]) == 2


def test_reasoning_trace_omitted_when_no_rounds():
    """Regression guard: no react_trace_rounds on ctx -> key absent, not []."""
    ctx = _make_ctx()
    card = {"mode": "FACTUAL", "direct_answer": "A normal answer.", "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert "reasoning_trace" not in payload


def test_reasoning_trace_skips_rounds_without_enrichment():
    """A round the LLM never enriched (e.g. is_complete on round 1) is
    skipped, not padded with an empty entry."""
    rounds = [
        {"round": 1, "tool": "search_corpus"},  # no "enrichment" key at all
        _TRACE_ROUNDS[0],
    ]
    ctx = _make_ctx(react_trace_rounds=rounds)
    card = {"mode": "FACTUAL", "direct_answer": "A normal answer.", "sections": []}
    with patch("app.stages.integrate.format_response") as mock_format:
        mock_format.return_value = (json.dumps(card), None)
        run_integrate(ctx)

    payload = json.loads(ctx.response_payload["message"])
    assert len(payload["reasoning_trace"]) == 1
    assert payload["reasoning_trace"][0]["round"] == 1
    assert payload["reasoning_trace"][0]["running_answer"] == "Filing deadline is likely 180 days."
