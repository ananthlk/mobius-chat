"""Task #86 (2026-08-11, Chat Master): a 429/timeout/provider_error that
survives generate_sync's own retry+fallback machinery must escalate to a
genuine turn failure, not silently complete with degraded content.

Root cause (live incident): non_patient_rag.py's RAG-answering LLM call
classified the exception via classify_exception (producing "The model is
temporarily busy — trying another option."), then kept going -- set
answer = "[rate_limit]" and returned a normal, "successful" tuple. That
flowed through the whole pipeline as a completed turn: no retry button
(only orchestrator.py's _publish_failed sets retryable=True), a stale
"Research" tool-attribution label, and copy implying automatic failover
succeeded when the turn had actually failed.

Fix: answer_non_patient stamps ctx.llm_provider_exhausted (a side-channel
signal, its own return contract for other callers is unchanged) when the
error is recoverable. react_loop.py's main round loop checks that signal
immediately after each tool call, BEFORE retry_guard.record_result would
absorb it as just another tool failure, and raises -- which propagates
cleanly out of run_react() to orchestrator.py's existing try/except
(calls _publish_failed, which already computes retryable correctly).
"""
from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from app.pipeline.context import PipelineContext


class TestContextFieldDefaults:
    def test_llm_provider_exhausted_defaults_none(self):
        ctx = PipelineContext(correlation_id="c", thread_id=None, message="hi")
        assert ctx.llm_provider_exhausted is None


class TestAnswerNonPatientSignalsRecoverableErrors:
    """answer_non_patient's return contract is unchanged for existing
    callers (still returns a degraded-but-valid tuple) -- the signal is
    purely additive, read via a stubbed error_emit module since the real
    mobius_contracts package isn't installed in this dev sandbox (a
    pre-existing environment gap affecting other tests in this suite,
    e.g. test_tool_auto_retry.py)."""

    def _stub_error_emit(self, *, error_code: str, is_recoverable: bool):
        """Install a fake app.communication.error_emit module exposing a
        classify_exception that returns a minimal duck-typed envelope --
        avoids importing the real (missing) mobius_contracts package."""
        class _FakeEnv:
            def __init__(self):
                self.error_code = error_code
                self.is_recoverable = is_recoverable
                self.user_facing_message = "The model is temporarily busy — trying another option."
                self.internal_detail = f"{error_code}: simulated provider failure"

        fake_module = types.ModuleType("app.communication.error_emit")
        fake_module.classify_exception = lambda exc, tool=None, round=None: _FakeEnv()
        return patch.dict(sys.modules, {"app.communication.error_emit": fake_module})

    def test_recoverable_error_sets_signal_on_ctx(self):
        ctx = PipelineContext(correlation_id="c", thread_id=None, message="hi")
        with self._stub_error_emit(error_code="rate_limit", is_recoverable=True):
            with patch("app.services.llm_manager.generate_sync", side_effect=RuntimeError("429 Resource exhausted")):
                from app.services.non_patient_rag import answer_non_patient
                answer, sources, usage, signal = answer_non_patient(
                    question="What is the timely filing deadline?",
                    pipeline_ctx=ctx,
                )
        assert ctx.llm_provider_exhausted is not None
        assert ctx.llm_provider_exhausted.error_code == "rate_limit"
        # Return contract unchanged -- still a valid tuple, not an exception.
        assert isinstance(answer, str)

    def test_non_recoverable_error_does_not_set_signal(self):
        """Content-filter/refusal-type errors are NOT recoverable -- must
        not trigger the same hard-stop escalation, that's a different UX
        (soft degrade is correct there)."""
        ctx = PipelineContext(correlation_id="c", thread_id=None, message="hi")
        with self._stub_error_emit(error_code="refusal", is_recoverable=False):
            with patch("app.services.llm_manager.generate_sync", side_effect=RuntimeError("blocked by content filtering policy")):
                from app.services.non_patient_rag import answer_non_patient
                answer_non_patient(question="hi", pipeline_ctx=ctx)
        assert ctx.llm_provider_exhausted is None

    def test_no_pipeline_ctx_does_not_raise(self):
        """Existing callers that don't pass pipeline_ctx must be
        completely unaffected -- signal-setting is best-effort additive."""
        with self._stub_error_emit(error_code="rate_limit", is_recoverable=True):
            with patch("app.services.llm_manager.generate_sync", side_effect=RuntimeError("429")):
                from app.services.non_patient_rag import answer_non_patient
                answer, sources, usage, signal = answer_non_patient(question="hi")
        assert isinstance(answer, str)


class TestReactLoopEscalatesOnSignal:
    def _make_ctx(self) -> PipelineContext:
        ctx = PipelineContext(correlation_id="c-exhausted", thread_id=None, message="What is the timely filing deadline?")
        ctx.effective_message = ctx.message
        ctx.merged_state = {}
        ctx.last_turns = []
        return ctx

    def test_signal_raises_before_retry_guard_absorbs_it(self):
        """The exact bug: without this check, a recoverable-error tool
        result just gets recorded by retry_guard as one more failed
        attempt and the loop keeps going (eventually completing with
        degraded content). The signal must short-circuit BEFORE that."""
        from app.pipeline.react_loop import run_react

        ctx = self._make_ctx()

        class _FakeEnv:
            internal_detail = "429 Resource exhausted: simulated"

        def fake_execute(tool, inputs, ctx, round_num, emit_fn, tool_emitter, skip_retry=False):
            # Simulates what answer_non_patient does inside the real tool call.
            ctx.llm_provider_exhausted = _FakeEnv()
            return {
                "tool": "search_corpus", "success": False,
                "result": "[rate_limit]", "signal": "no_sources", "sources": [], "usage": None,
            }

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            return '{"thought": "Search first.", "tool": "search_corpus", "inputs": {"query": "timely filing"}, "is_complete": false}'

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", side_effect=fake_execute):
            with pytest.raises(RuntimeError, match="429 Resource exhausted"):
                run_react(ctx, emitter=None)

    def test_no_signal_normal_completion_unaffected(self):
        """Regression guard: the common case (no signal set) must behave
        exactly as before -- no raise, normal completion."""
        from app.pipeline.react_loop import run_react

        ctx = self._make_ctx()

        def fake_execute(tool, inputs, ctx, round_num, emit_fn, tool_emitter, skip_retry=False):
            return {
                "tool": "search_corpus", "success": True,
                "result": "Timely filing is 180 days.", "signal": "corpus_only",
                "sources": [{"document_name": "Manual", "index": 1, "text": "180 days"}],
                "usage": None, "is_terminal": True,
            }

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            return '{"thought": "Search first.", "tool": "search_corpus", "inputs": {"query": "timely filing"}, "is_complete": false}'

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm), \
             patch("app.pipeline.react_loop._execute_tool_with_retry", side_effect=fake_execute):
            run_react(ctx, emitter=None)

        assert ctx.final_message == "Timely filing is 180 days."
