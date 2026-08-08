"""Tests for run_bc_background (Task #76, 2026-08-08): fire-and-forget
critic+enrichment calls on a persistent (non-request-scoped) executor.
Submits and returns immediately; each call's done-callback patches the
already-persisted card via PersistencePort.patch_turn_card + fires the
existing integrator_partial progress event.
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

from app.responder.final_parallel import run_bc_background


def _mock_prompts():
    mock_prompts = MagicMock()
    mock_prompts.integrator_parallel_critic_system = "critic sys"
    mock_prompts.integrator_parallel_enrichment_system = "enrichment sys"
    mock_prompts.integrator_user_template = "Input:\n{consolidator_input_json}\n\nReturn JSON."
    return mock_prompts


def _wait_for(predicate, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestRunBcBackground:
    def test_returns_immediately_without_waiting_for_calls(self):
        """The whole point: submitting must not block on the LLM calls."""
        def slow_call(prompt, stage="integrator_critic", max_tokens=3072, **kw):
            time.sleep(0.5)
            return ("{}", {"stage": stage, "model": "m", "input_tokens": 0, "output_tokens": 0})

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=slow_call),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            t0 = time.monotonic()
            run_bc_background('{"q": "x"}', "cid-bg-1", {"config_sha": None, "correlation_id": "cid-bg-1", "thread_id": None, "phi_detected": False, "mode": None})
            elapsed = time.monotonic() - t0

        assert elapsed < 0.3, f"run_bc_background blocked for {elapsed}s -- should return immediately"

    def test_critic_result_patches_persisted_card(self):
        critic_json = json.dumps({
            "citations": [{"claim": "X", "doc_title": "Doc"}],
            "cited_source_indices": [1],
            "takeaways": ["Remember X."],
            "gaps": [],
        })

        def fake(prompt, stage="integrator_critic", max_tokens=3072, **kw):
            if stage == "integrator_critic":
                return (critic_json, {"stage": stage, "model": "m", "input_tokens": 0, "output_tokens": 0})
            return ("{}", {"stage": stage, "model": "m", "input_tokens": 0, "output_tokens": 0})

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fake),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
            patch("app.persistence.get_persistence") as mock_get_persist,
            patch("app.storage.progress.append_integrator_partial") as mock_partial,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            mock_persist = MagicMock()
            mock_get_persist.return_value = mock_persist
            run_bc_background('{"q": "x"}', "cid-bg-2", {"config_sha": None, "correlation_id": "cid-bg-2", "thread_id": None, "phi_detected": False, "mode": None})

            assert _wait_for(lambda: mock_persist.patch_turn_card.called)

        patch_arg = mock_persist.patch_turn_card.call_args.args
        assert patch_arg[0] == "cid-bg-2"
        assert patch_arg[1]["citations"] == [{"claim": "X", "doc_title": "Doc"}]
        assert patch_arg[1]["takeaways"] == ["Remember X."]
        assert mock_partial.called
        assert mock_partial.call_args.args[0] == "cid-bg-2"
        assert mock_partial.call_args.args[1] == "citations"

    def test_enrichment_result_patches_persisted_card(self):
        enrich_json = json.dumps({
            "next_questions_for_user": ["What is Y?"],
            "next_steps": ["Submit within 90 days."],
            "suggested_actions": [],
        })

        def fake(prompt, stage="integrator_critic", max_tokens=3072, **kw):
            if stage == "integrator_enrichment":
                return (enrich_json, {"stage": stage, "model": "m", "input_tokens": 0, "output_tokens": 0})
            return ("{}", {"stage": stage, "model": "m", "input_tokens": 0, "output_tokens": 0})

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fake),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
            patch("app.persistence.get_persistence") as mock_get_persist,
            patch("app.storage.progress.append_integrator_partial") as mock_partial,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            mock_persist = MagicMock()
            mock_get_persist.return_value = mock_persist
            run_bc_background('{"q": "x"}', "cid-bg-3", {"config_sha": None, "correlation_id": "cid-bg-3", "thread_id": None, "phi_detected": False, "mode": None})

            assert _wait_for(lambda: any(
                c.args[1].get("next_steps") for c in mock_persist.patch_turn_card.call_args_list
            ))

        found = [c for c in mock_persist.patch_turn_card.call_args_list if c.args[1].get("next_steps")]
        assert found
        assert found[0].args[1]["next_steps"] == ["Submit within 90 days."]

    def test_call_failure_does_not_raise_or_patch(self):
        def fail(prompt, stage="integrator_critic", max_tokens=3072, **kw):
            raise RuntimeError("LLM down")

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fail),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
            patch("app.persistence.get_persistence") as mock_get_persist,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            mock_persist = MagicMock()
            mock_get_persist.return_value = mock_persist
            run_bc_background('{"q": "x"}', "cid-bg-4", {"config_sha": None, "correlation_id": "cid-bg-4", "thread_id": None, "phi_detected": False, "mode": None})
            time.sleep(0.3)

        mock_persist.patch_turn_card.assert_not_called()

    def test_malformed_correction_in_background_critic_is_dropped(self):
        critic_json = json.dumps({
            "citations": [], "cited_source_indices": [], "takeaways": [], "gaps": [],
            "correction": {"original": "180 days"},  # missing "corrected"
        })

        def fake(prompt, stage="integrator_critic", max_tokens=3072, **kw):
            if stage == "integrator_critic":
                return (critic_json, {"stage": stage, "model": "m", "input_tokens": 0, "output_tokens": 0})
            return ("{}", {"stage": stage, "model": "m", "input_tokens": 0, "output_tokens": 0})

        with (
            patch("app.responder.final_parallel._call_llm", side_effect=fake),
            patch("app.responder.final_parallel.get_chat_config") as mock_cfg,
            patch("app.persistence.get_persistence") as mock_get_persist,
        ):
            mock_cfg.return_value.prompts = _mock_prompts()
            mock_persist = MagicMock()
            mock_get_persist.return_value = mock_persist
            run_bc_background('{"q": "x"}', "cid-bg-5", {"config_sha": None, "correlation_id": "cid-bg-5", "thread_id": None, "phi_detected": False, "mode": None})
            time.sleep(0.3)

        # No patch call at all since citations/indices/takeaways/gaps are all empty
        # and correction is malformed -- nothing worth patching.
        if mock_persist.patch_turn_card.called:
            for c in mock_persist.patch_turn_card.call_args_list:
                assert "correction" not in c.args[1]
