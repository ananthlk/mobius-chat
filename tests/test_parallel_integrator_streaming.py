"""Tests for progressive parallel-integrator streaming
(docs/SPEC_PARALLEL_INTEGRATOR_STREAMING.md, 2026-08-08).

append_integrator_partial (app/storage/progress.py) fires "integrator_partial"
events, part-discriminated ("core"|"citations"|"enrichment"), the moment EACH
of the 3 concurrent calls completes -- fixing a real gap where the prior code
waited for as_completed() to yield all three before parsing/emitting anything,
so the client saw nothing until the slowest call finished despite the calls
running concurrently."""
from __future__ import annotations

from unittest.mock import patch

from app.storage.progress import _lock, _progress, append_integrator_partial


def test_append_integrator_partial_fires_event_with_part_and_data():
    cid = "partial-event-test"
    with _lock:
        _progress[cid] = {"events": []}
    try:
        with patch("app.storage.progress._publish_progress_event") as mock_publish:
            append_integrator_partial(cid, "core", {"mode": "FACTUAL", "direct_answer": "x"})
        with _lock:
            events = list(_progress[cid]["events"])
        assert len(events) == 1
        assert events[0]["event"] == "integrator_partial"
        assert events[0]["data"]["part"] == "core"
        assert events[0]["data"]["mode"] == "FACTUAL"
        assert events[0]["data"]["direct_answer"] == "x"
        mock_publish.assert_called_once()
    finally:
        with _lock:
            _progress.pop(cid, None)


def test_append_integrator_partial_citations_part():
    cid = "partial-event-citations"
    with _lock:
        _progress[cid] = {"events": []}
    try:
        with patch("app.storage.progress._publish_progress_event"):
            append_integrator_partial(cid, "citations", {"citations": [{"claim": "x"}], "gaps": []})
        with _lock:
            events = list(_progress[cid]["events"])
        assert events[0]["data"]["part"] == "citations"
        assert events[0]["data"]["citations"] == [{"claim": "x"}]
    finally:
        with _lock:
            _progress.pop(cid, None)


def test_append_integrator_partial_enrichment_part():
    cid = "partial-event-enrichment"
    with _lock:
        _progress[cid] = {"events": []}
    try:
        with patch("app.storage.progress._publish_progress_event"):
            append_integrator_partial(cid, "enrichment", {"next_steps": ["Submit form."]})
        with _lock:
            events = list(_progress[cid]["events"])
        assert events[0]["data"]["part"] == "enrichment"
        assert events[0]["data"]["next_steps"] == ["Submit form."]
    finally:
        with _lock:
            _progress.pop(cid, None)


def test_append_integrator_partial_noop_when_correlation_id_not_tracked():
    """No entry in _progress[cid] (e.g. turn already finalized) -- must not
    raise, and _publish_progress_event still fires (Redis fan-out doesn't
    depend on the local dict, same pattern as append_draft_answer)."""
    cid = "untracked-cid"
    with _lock:
        _progress.pop(cid, None)
    with patch("app.storage.progress._publish_progress_event") as mock_publish:
        append_integrator_partial(cid, "core", {"mode": "FACTUAL"})
    mock_publish.assert_called_once()


def test_append_integrator_partial_skips_empty_data():
    """No content to stream -- no event should be constructed with an empty
    data dict flooding the client for nothing meaningful; the function
    itself still fires (callers guard emptiness), but confirm the shape is
    exactly {"part": ..., **data} with no extra keys."""
    cid = "partial-event-shape"
    with _lock:
        _progress[cid] = {"events": []}
    try:
        with patch("app.storage.progress._publish_progress_event"):
            append_integrator_partial(cid, "core", {"mode": "FACTUAL"})
        with _lock:
            events = list(_progress[cid]["events"])
        assert events[0]["data"] == {"part": "core", "mode": "FACTUAL"}
    finally:
        with _lock:
            _progress.pop(cid, None)


# ── format_response_parallel emits partials per call ────────────────────────

import json
from typing import Any
from unittest.mock import MagicMock, patch

from app.planner.schemas import Plan, SubQuestion
from app.responder.final_parallel import format_response_parallel

VALID_CORE_CARD = json.dumps({
    "mode": "FACTUAL",
    "direct_answer": "X is a test answer.",
    "sections": [{"intent": "references", "label": "Details", "format": "bullets", "bullets": ["Bullet 1"]}],
    "thread_summary": "Test query — X",
})
VALID_CRITIC = json.dumps({
    "citations": [{"claim": "X is a test", "doc_title": "Doc A", "locator": "p.1", "snippet": "verbatim text"}],
    "gaps": [],
})
VALID_ENRICHMENT = json.dumps({
    "next_questions_for_user": ["What is Y?"],
    "next_steps": ["Submit within 90 days."],
    "suggested_actions": [],
})


def _fake_generate_sync(prompt: str, stage: str = "integrator_a", max_tokens: int = 4096, **kwargs) -> tuple[str, dict[str, Any]]:
    usage = {"stage": stage, "model": "test-model", "input_tokens": 10, "output_tokens": 20, "latency_ms": 100}
    if stage == "integrator_a":
        return (VALID_CORE_CARD, usage)
    if stage == "integrator_critic":
        return (VALID_CRITIC, usage)
    if stage == "integrator_enrichment":
        return (VALID_ENRICHMENT, usage)
    return ("{}", usage)


def _mock_cfg_prompts():
    mock_prompts = MagicMock()
    mock_prompts.consolidator_factual_max = 0.4
    mock_prompts.consolidator_canonical_min = 0.6
    mock_prompts.integrator_parallel_core_system = "core sys"
    mock_prompts.integrator_parallel_critic_system = "critic sys"
    mock_prompts.integrator_parallel_enrichment_system = "enrichment sys"
    mock_prompts.integrator_user_template = "Input:\n{consolidator_input_json}\n\nReturn JSON."
    return mock_prompts


class TestFormatResponseParallelStreamsPartials:
    def test_all_three_parts_emitted(self):
        plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
            patch("app.storage.progress.append_integrator_partial") as mock_partial,
        ):
            mock_cfg.return_value.prompts = _mock_cfg_prompts()
            format_response_parallel(plan, ["answer"], user_message="What is X?", correlation_id="cid-123")

        parts_emitted = {call.args[1] for call in mock_partial.call_args_list}
        assert parts_emitted == {"core", "citations", "enrichment"}

    def test_core_partial_carries_direct_answer_and_sections(self):
        plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
            patch("app.storage.progress.append_integrator_partial") as mock_partial,
        ):
            mock_cfg.return_value.prompts = _mock_cfg_prompts()
            format_response_parallel(plan, ["answer"], user_message="What is X?", correlation_id="cid-123")

        core_call = next(c for c in mock_partial.call_args_list if c.args[1] == "core")
        data = core_call.args[2]
        assert data["direct_answer"] == "X is a test answer."
        assert len(data["sections"]) == 1

    def test_citations_partial_carries_citations(self):
        plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
            patch("app.storage.progress.append_integrator_partial") as mock_partial,
        ):
            mock_cfg.return_value.prompts = _mock_cfg_prompts()
            format_response_parallel(plan, ["answer"], user_message="What is X?", correlation_id="cid-123")

        citations_call = next(c for c in mock_partial.call_args_list if c.args[1] == "citations")
        assert citations_call.args[2]["citations"][0]["claim"] == "X is a test"

    def test_enrichment_partial_carries_next_steps(self):
        plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
            patch("app.storage.progress.append_integrator_partial") as mock_partial,
        ):
            mock_cfg.return_value.prompts = _mock_cfg_prompts()
            format_response_parallel(plan, ["answer"], user_message="What is X?", correlation_id="cid-123")

        enrichment_call = next(c for c in mock_partial.call_args_list if c.args[1] == "enrichment")
        assert enrichment_call.args[2]["next_steps"] == ["Submit within 90 days."]

    def test_no_partial_emitted_when_call_a_fails(self):
        plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])

        def fail_a(prompt, stage, max_tokens, **kwargs):
            if stage == "integrator_a":
                raise RuntimeError("A failed")
            return _fake_generate_sync(prompt, stage, max_tokens, **kwargs)

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fail_a),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
            patch("app.storage.progress.append_integrator_partial") as mock_partial,
        ):
            mock_cfg.return_value.prompts = _mock_cfg_prompts()
            format_response_parallel(plan, ["answer"], user_message="What is X?", correlation_id="cid-123")

        parts_emitted = {call.args[1] for call in mock_partial.call_args_list}
        assert "core" not in parts_emitted
        # B and C still stream independently -- A failing doesn't block them.
        assert "citations" in parts_emitted
        assert "enrichment" in parts_emitted

    def test_latency_budgets_passed_to_each_call(self):
        """Confirms the latency_budget_ms hard-filter values actually reach
        _call_llm for each of the 3 stages (Task: latency reduction, see
        docs/SPEC_PARALLEL_INTEGRATOR_STREAMING.md)."""
        plan = Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])
        seen_budgets = {}

        def capture(prompt, stage, max_tokens, **kwargs):
            seen_budgets[stage] = kwargs.get("latency_budget_ms")
            return _fake_generate_sync(prompt, stage, max_tokens, **kwargs)

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=capture),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_cfg_prompts()
            format_response_parallel(plan, ["answer"], user_message="What is X?", correlation_id="cid-123")

        assert seen_budgets["integrator_a"] == 3000
        assert seen_budgets["integrator_critic"] == 2000
        assert seen_budgets["integrator_enrichment"] == 1500
