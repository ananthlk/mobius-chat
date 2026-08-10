"""Tests for the parallel integrator path (final_parallel.py + integrate.py routing)."""
from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.context import PipelineContext
from app.planner.schemas import Plan, SubQuestion
from app.responder.final import _build_consolidator_input_json
from app.responder.final_parallel import _parse_json_response, format_response_parallel


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_plan() -> Plan:
    return Plan(subquestions=[SubQuestion(id="sq1", text="What is X?", kind="non_patient")])


def _make_ctx() -> PipelineContext:
    return PipelineContext(
        correlation_id="test-cid",
        thread_id="test-thread",
        message="What is X?",
        plan=_make_plan(),
        answers=["Some answer about X."],
        sources=[],
        usages=[],
        retrieval_signals=[],
    )


VALID_CORE_CARD = json.dumps({
    "mode": "FACTUAL",
    "direct_answer": "X is a test answer.",
    "sections": [{"intent": "references", "label": "Details", "format": "bullets", "bullets": ["Bullet 1"]}],
    "thread_summary": "Test query — X",
})

VALID_CRITIC = json.dumps({
    "citations": [{"claim": "X is a test", "doc_title": "Doc A", "locator": "p.1", "snippet": "verbatim text"}],
    "cited_source_indices": [1],
    "source_confidence_override": None,
    "confidence_note": None,
    "takeaways": ["Remember X."],
    "gaps": [],
})

VALID_ENRICHMENT = json.dumps({
    "next_questions_for_user": ["What is Y?", "How does X relate to Z?"],
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


# ── Unit tests ────────────────────────────────────────────────────────────────

class TestParseJsonResponse:
    def test_valid_json(self):
        result = _parse_json_response('{"key": "value"}', "test")
        assert result == {"key": "value"}

    def test_json_with_fence(self):
        result = _parse_json_response('```json\n{"key": "value"}\n```', "test")
        assert result == {"key": "value"}

    def test_empty_string(self):
        assert _parse_json_response("", "test") == {}

    def test_malformed_returns_empty(self):
        result = _parse_json_response("not json at all {{{{", "test")
        assert isinstance(result, dict)


class TestFormatResponseParallel:
    def test_happy_path_merges_all_three(self):
        plan = _make_plan()
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_prompts = MagicMock()
            mock_prompts.consolidator_factual_max = 0.4
            mock_prompts.consolidator_canonical_min = 0.6
            mock_prompts.integrator_parallel_core_system = "core sys"
            mock_prompts.integrator_parallel_critic_system = "critic sys"
            mock_prompts.integrator_parallel_enrichment_system = "enrichment sys"
            mock_prompts.integrator_user_template = "Input:\n{consolidator_input_json}\n\nReturn JSON."
            mock_cfg.return_value.prompts = mock_prompts

            result_json, usages = format_response_parallel(
                plan, ["answer"], user_message="What is X?"
            )

        card = json.loads(result_json)
        # Core fields present
        assert card["mode"] == "FACTUAL"
        assert "X is a test answer." in card["direct_answer"]
        # Critic fields merged
        assert len(card["citations"]) == 1
        assert card["citations"][0]["claim"] == "X is a test"
        assert card["takeaways"] == ["Remember X."]
        assert card["gaps"] == []
        # Enrichment fields merged
        assert "What is Y?" in card["next_questions_for_user"]
        assert card["next_steps"] == ["Submit within 90 days."]
        # 3 usage dicts returned
        assert len(usages) == 3
        assert {u["stage"] for u in usages} == {"integrator_a", "integrator_critic", "integrator_enrichment"}

    def test_fallback_when_core_fails(self):
        plan = _make_plan()

        def fail_a(prompt, stage="integrator_a", max_tokens=4096, **kw):
            if stage == "integrator_a":
                raise RuntimeError("LLM failure")
            return ("{}", {"stage": stage, "model": "m", "input_tokens": 0, "output_tokens": 0, "latency_ms": 0})

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fail_a),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_prompts = MagicMock()
            mock_prompts.consolidator_factual_max = 0.4
            mock_prompts.consolidator_canonical_min = 0.6
            mock_prompts.integrator_parallel_core_system = "core sys"
            mock_prompts.integrator_parallel_critic_system = "critic sys"
            mock_prompts.integrator_parallel_enrichment_system = "enrichment sys"
            mock_prompts.integrator_user_template = "Input:\n{consolidator_input_json}\n\nReturn JSON."
            mock_cfg.return_value.prompts = mock_prompts

            result_json, usages = format_response_parallel(
                plan, ["answer"], user_message="What is X?"
            )

        # ThreadPoolExecutor itself shouldn't crash; result should be a string
        assert isinstance(result_json, str)

    def test_critic_failure_doesnt_break_core(self):
        """B fails; A+C still succeed; card has no citations but has followups."""
        plan = _make_plan()

        def partial_fail(prompt, stage="integrator_a", max_tokens=4096, **kw):
            if stage == "integrator_critic":
                raise RuntimeError("critic down")
            return _fake_generate_sync(prompt, stage, max_tokens, **kw)

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=partial_fail),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_prompts = MagicMock()
            mock_prompts.consolidator_factual_max = 0.4
            mock_prompts.consolidator_canonical_min = 0.6
            mock_prompts.integrator_parallel_core_system = "core sys"
            mock_prompts.integrator_parallel_critic_system = "critic sys"
            mock_prompts.integrator_parallel_enrichment_system = "enrichment sys"
            mock_prompts.integrator_user_template = "Input:\n{consolidator_input_json}\n\nReturn JSON."
            mock_cfg.return_value.prompts = mock_prompts

            result_json, usages = format_response_parallel(
                plan, ["answer"], user_message="What is X?"
            )

        card = json.loads(result_json)
        assert card["mode"] == "FACTUAL"
        assert "What is Y?" in card.get("next_questions_for_user", [])
        # citations absent (B failed) — card still valid
        assert "direct_answer" in card


def _mock_prompts():
    mock_prompts = MagicMock()
    mock_prompts.consolidator_factual_max = 0.4
    mock_prompts.consolidator_canonical_min = 0.6
    mock_prompts.integrator_parallel_core_system = "core sys"
    mock_prompts.integrator_parallel_critic_system = "critic sys"
    mock_prompts.integrator_parallel_enrichment_system = "enrichment sys"
    mock_prompts.integrator_user_template = "Input:\n{consolidator_input_json}\n\nReturn JSON."
    return mock_prompts


class TestUserPerspective:
    """2026-08-08, Ananth directly: role-aware "quick glance" emphasis.
    user_perspective (provider_office/patient, from active.user_role via
    jurisdiction.perspective) rides the consolidator input JSON so Call A can
    pick which facts to bold/hero-card/lead with for that reader."""

    def test_included_when_present(self):
        raw = _build_consolidator_input_json(
            _make_plan(), [], "What is X?", user_perspective="provider_office",
        )
        payload = json.loads(raw)
        assert payload.get("user_perspective") == "provider_office"

    def test_omitted_when_absent(self):
        raw = _build_consolidator_input_json(_make_plan(), [], "What is X?")
        payload = json.loads(raw)
        assert "user_perspective" not in payload

    def test_omitted_when_blank(self):
        raw = _build_consolidator_input_json(
            _make_plan(), [], "What is X?", user_perspective="   ",
        )
        payload = json.loads(raw)
        assert "user_perspective" not in payload

    def test_reaches_call_a_prompt_via_format_response_parallel(self):
        """End-to-end: format_response_parallel must actually pass
        user_perspective through to the consolidator input, not just accept
        it as a dead kwarg."""
        plan = _make_plan()
        captured = {}

        def fake(prompt, stage="integrator_a", max_tokens=4096, **kw):
            if stage == "integrator_a":
                captured["prompt"] = prompt
            return _fake_generate_sync(prompt, stage, max_tokens, **kw)

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fake),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            format_response_parallel(
                plan, ["answer"], user_message="What is X?", user_perspective="patient",
            )

        assert '"user_perspective": "patient"' in captured["prompt"]


class TestSourcesForInlineCitations:
    """2026-08-08, Chat FE inline [N] citation footnotes. card.sources[] is
    built positionally from the SAME rag_chunks list Call A saw -- 1st
    rag_chunks entry = sources[0] = marker [1] -- NOT keyed by rag_chunks'
    own (possibly stale/duplicated after multi-round dedup) 'index' field."""

    def test_sources_built_positionally_from_rag_chunks(self):
        plan = _make_plan()
        rag_chunks = [
            {"index": 1, "document_name": "policy_a.pdf", "page_number": 4, "text": "Initial filing is 180 days from date of service."},
            {"index": 1, "document_name": "policy_b.pdf", "page_number": 2, "text": "Resubmission window is 90 days."},
        ]
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            result_json, _ = format_response_parallel(
                plan, ["answer"], user_message="What is X?", rag_chunks=rag_chunks,
            )

        card = json.loads(result_json)
        assert len(card["sources"]) == 2
        # positional, not keyed by the (duplicate) "index" field above
        assert card["sources"][0]["document_name"] == "policy_a.pdf"
        assert card["sources"][0]["locator"] == "p. 4"
        assert card["sources"][1]["document_name"] == "policy_b.pdf"
        assert card["sources"][1]["locator"] == "p. 2"

    def test_snippet_truncated_and_present(self):
        plan = _make_plan()
        rag_chunks = [{"document_name": "doc.pdf", "text": "x" * 500}]
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            result_json, _ = format_response_parallel(
                plan, ["answer"], user_message="What is X?", rag_chunks=rag_chunks,
            )

        card = json.loads(result_json)
        assert len(card["sources"][0]["snippet"]) == 300

    def test_no_rag_chunks_no_sources_key(self):
        plan = _make_plan()
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            result_json, _ = format_response_parallel(plan, ["answer"], user_message="What is X?")

        card = json.loads(result_json)
        assert "sources" not in card

    def test_missing_page_number_gives_null_locator(self):
        plan = _make_plan()
        rag_chunks = [{"document_name": "doc.pdf", "text": "x"}]
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            result_json, _ = format_response_parallel(
                plan, ["answer"], user_message="What is X?", rag_chunks=rag_chunks,
            )

        card = json.loads(result_json)
        assert card["sources"][0]["locator"] is None

    def test_document_id_and_raw_page_number_carried_for_doc_reader_deep_link(self):
        """openDocReaderPanel(documentId, pageNumber, citeText) (app.ts:4160)
        needs the RAW document_id + page_number, not just the "p. N" display
        locator string."""
        plan = _make_plan()
        rag_chunks = [{"document_name": "doc.pdf", "document_id": "doc-abc-123", "page_number": 7, "text": "x"}]
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            result_json, _ = format_response_parallel(
                plan, ["answer"], user_message="What is X?", rag_chunks=rag_chunks,
            )

        card = json.loads(result_json)
        assert card["sources"][0]["document_id"] == "doc-abc-123"
        assert card["sources"][0]["page_number"] == 7

    def test_sources_reaches_full_card_via_integrate_stage(self):
        """End-to-end: run_integrate must carry sources through the same
        allowlist-copy mechanism as correction/reasoning_trace/
        cta_confirm_authoritative -- added to the allowlist FIRST this time."""
        from app.stages.integrate import run_integrate

        ctx = _make_ctx()
        card_with_sources = json.dumps({
            "mode": "FACTUAL", "direct_answer": "180 days [1].", "sections": [],
            "sources": [{"document_name": "policy.pdf", "locator": "p. 4", "snippet": "..."}],
        })

        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel"}),
            patch("app.stages.integrate.format_response_parallel") as mock_par,
        ):
            mock_par.return_value = (card_with_sources, [{"stage": "integrator_a", "model": "m", "input_tokens": 0, "output_tokens": 0}])
            run_integrate(ctx)

        payload = json.loads(ctx.response_payload["message"])
        assert payload.get("sources") == [{"document_name": "policy.pdf", "locator": "p. 4", "snippet": "..."}]


class TestCorrectionMerge:
    """2026-08-08, Chat FE: inline redline {original, corrected} -- Chat Master
    directive. Critic (Call B) is the evidence-verification pass, so it's the
    right place to detect react_draft/Call A claims that rag_chunks/
    tool_outputs actually contradict."""

    def test_critic_correction_merged_into_card(self):
        plan = _make_plan()

        def fake(prompt, stage="integrator_a", max_tokens=4096, **kw):
            if stage == "integrator_critic":
                return (json.dumps({
                    "citations": [], "cited_source_indices": [], "takeaways": [], "gaps": [],
                    "correction": {"original": "180 days", "corrected": "90 days"},
                }), {"stage": stage, "model": "m", "input_tokens": 1, "output_tokens": 1, "latency_ms": 1})
            return _fake_generate_sync(prompt, stage, max_tokens, **kw)

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fake),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            result_json, _ = format_response_parallel(plan, ["answer"], user_message="What is X?")

        card = json.loads(result_json)
        assert card["correction"] == {"original": "180 days", "corrected": "90 days"}

    def test_critic_null_correction_does_not_add_key(self):
        plan = _make_plan()
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            result_json, _ = format_response_parallel(plan, ["answer"], user_message="What is X?")

        card = json.loads(result_json)
        assert "correction" not in card

    def test_critic_null_correction_does_not_erase_call_a_correction(self):
        """Call A's own (rare, self-detected) correction survives when
        critic finds nothing -- a critic null must not silently overwrite it."""
        plan = _make_plan()
        core_with_correction = json.dumps({
            "mode": "FACTUAL",
            "direct_answer": "X is a test answer.",
            "sections": [],
            "correction": {"original": "wrong claim", "corrected": "right claim"},
        })

        def fake(prompt, stage="integrator_a", max_tokens=4096, **kw):
            if stage == "integrator_a":
                return (core_with_correction, {"stage": stage, "model": "m", "input_tokens": 1, "output_tokens": 1, "latency_ms": 1})
            return _fake_generate_sync(prompt, stage, max_tokens, **kw)

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fake),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            result_json, _ = format_response_parallel(plan, ["answer"], user_message="What is X?")

        card = json.loads(result_json)
        assert card["correction"] == {"original": "wrong claim", "corrected": "right claim"}

    def test_malformed_critic_correction_ignored(self):
        """A correction dict missing original/corrected is dropped, not
        passed through half-formed to the frontend redline."""
        plan = _make_plan()

        def fake(prompt, stage="integrator_a", max_tokens=4096, **kw):
            if stage == "integrator_critic":
                return (json.dumps({
                    "citations": [], "cited_source_indices": [], "takeaways": [], "gaps": [],
                    "correction": {"original": "180 days"},
                }), {"stage": stage, "model": "m", "input_tokens": 1, "output_tokens": 1, "latency_ms": 1})
            return _fake_generate_sync(prompt, stage, max_tokens, **kw)

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fake),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            result_json, _ = format_response_parallel(plan, ["answer"], user_message="What is X?")

        card = json.loads(result_json)
        assert "correction" not in card

    def test_correction_reaches_full_card_via_integrate_stage(self):
        """End-to-end: run_integrate (the real allowlist-copy path) must
        still carry correction through, same as react_draft/suggest_escalate/
        cta_confirm_authoritative did before this -- correction IS in
        _ANSWER_CARD_ENVELOPE_KEYS, confirming the plumbing survives the
        same allowlist-copy mechanism that dropped those other fields
        before they were added to it."""
        from app.stages.integrate import run_integrate

        ctx = _make_ctx()
        card_with_correction = json.dumps({
            "mode": "FACTUAL", "direct_answer": "X is a test answer.", "sections": [],
            "correction": {"original": "180 days", "corrected": "90 days"},
        })

        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel"}),
            patch("app.stages.integrate.format_response_parallel") as mock_par,
        ):
            mock_par.return_value = (card_with_correction, [{"stage": "integrator_a", "model": "m", "input_tokens": 0, "output_tokens": 0}])
            run_integrate(ctx)

        payload = json.loads(ctx.response_payload["message"])
        assert payload.get("correction") == {"original": "180 days", "corrected": "90 days"}


class TestPreBuiltSectionsSurviveParallel:
    """Chat FE trace (2026-08-10, cid=2803928f): the parallel path never
    called ensure_pre_built_sections -- deterministic tool-derived sections
    (e.g. appeals_playbook) silently vanished whenever the integrator's own
    JSON didn't happen to reproduce them verbatim. final.py's sequential-path
    fix for this exact symptom never got ported when parallel became the
    default integrator mode (MOBIUS_INTEGRATOR_MODE=parallel)."""

    def test_dropped_typed_section_reinjected(self):
        plan = _make_plan()
        hints = [{
            "section_format": "appeals_playbook",
            "section_title": "Playbook",
            "data": {"deadline": "90d", "channel": "portal"},
        }]
        with (
            patch("app.responder.final_parallel._call_llm", side_effect=_fake_generate_sync),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            result_json, _ = format_response_parallel(
                plan, ["answer"], user_message="What is X?", tool_section_hints=hints,
            )
        card = json.loads(result_json)
        sections = card.get("sections") or []
        matched = [s for s in sections if s.get("format") == "appeals_playbook"]
        assert matched, f"typed section missing from parallel-path card: {sections}"
        assert matched[0]["data"] == {"deadline": "90d", "channel": "portal"}

    def test_parse_failure_fallback_still_carries_typed_section(self):
        """Even when Call A's output fails to parse (fallback card path),
        tool-derived sections must not be lost -- same guarantee final.py
        gives its fallback cards."""
        plan = _make_plan()
        hints = [{
            "section_format": "appeals_playbook",
            "section_title": "Playbook",
            "data": {"deadline": "90d"},
        }]

        def fake_broken(prompt, stage="integrator_a", max_tokens=4096, **kw):
            if stage == "integrator_a":
                return (
                    "not valid json {{{",
                    {"stage": stage, "model": "m", "input_tokens": 0, "output_tokens": 0, "latency_ms": 1},
                )
            return _fake_generate_sync(prompt, stage, max_tokens, **kw)

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fake_broken),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            result_json, _ = format_response_parallel(
                plan, ["answer"], user_message="What is X?", tool_section_hints=hints,
            )
        card = json.loads(result_json)
        sections = card.get("sections") or []
        matched = [s for s in sections if s.get("format") == "appeals_playbook"]
        assert matched, f"typed section missing from fallback card: {sections}"


# ── Integration test: A/B routing in integrate.py ────────────────────────────

class TestIntegratorModeRouting:
    def test_sequential_path_sets_mode_S(self):
        from app.stages.integrate import run_integrate
        ctx = _make_ctx()

        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "sequential"}),
            patch("app.stages.integrate.format_response") as mock_seq,
            patch("app.stages.integrate.format_response_parallel") as mock_par,
        ):
            mock_seq.return_value = (VALID_CORE_CARD, {"stage": "integrator", "model": "m", "input_tokens": 0, "output_tokens": 0})
            run_integrate(ctx)

        assert ctx.integrator_mode == "S"
        mock_par.assert_not_called()
        mock_seq.assert_called_once()

    def test_parallel_path_sets_mode_P(self):
        from app.stages.integrate import run_integrate
        ctx = _make_ctx()

        fake_usage = {"stage": "integrator_a", "model": "m", "input_tokens": 0, "output_tokens": 0}

        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "parallel"}),
            patch("app.stages.integrate.format_response") as mock_seq,
            patch("app.stages.integrate.format_response_parallel") as mock_par,
        ):
            mock_par.return_value = (VALID_CORE_CARD, [fake_usage])
            run_integrate(ctx)

        assert ctx.integrator_mode == "P"
        mock_seq.assert_not_called()
        mock_par.assert_called_once()

    def test_llm_performance_carries_integrator_mode(self):
        from app.stages.integrate import run_integrate
        ctx = _make_ctx()

        with (
            patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "sequential"}),
            patch("app.stages.integrate.format_response") as mock_seq,
        ):
            mock_seq.return_value = (VALID_CORE_CARD, {"stage": "integrator", "model": "m", "input_tokens": 0, "output_tokens": 0, "latency_ms": 200})
            run_integrate(ctx)

        assert ctx.response_payload is not None
        perf = ctx.response_payload.get("llm_performance", {})
        assert perf.get("integrator_mode") == "S"

    def test_parallel_pct_env(self):
        """MOBIUS_INTEGRATOR_PARALLEL_PCT=100 always picks parallel."""
        from app.stages.integrate import _pick_integrator_mode
        with patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "", "MOBIUS_INTEGRATOR_PARALLEL_PCT": "100"}):
            modes = {_pick_integrator_mode() for _ in range(20)}
        assert modes == {"parallel"}

    def test_default_is_sequential(self):
        from app.stages.integrate import _pick_integrator_mode
        with patch.dict(os.environ, {"MOBIUS_INTEGRATOR_MODE": "", "MOBIUS_INTEGRATOR_PARALLEL_PCT": "0"}):
            modes = {_pick_integrator_mode() for _ in range(10)}
        assert modes == {"sequential"}
