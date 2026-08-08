"""routing_keys.clarify_questions terminal signal (2026-08-08, Chat Master
directive).

Retriever populates routing_keys.clarify_questions when its own posture
classification is CLARIFY/CLARIFY_REPHRASE -- the corpus genuinely can't
answer without the user disambiguating first. Different signal from plain
"nothing found" (status=no_retrieval with clarify_questions empty still goes
through the normal relax/reframe protocol). More searching can't close a
disambiguation gap, so react must treat it as terminal: stop the loop
immediately (same react_bypass_integrate mechanism the "refuse" tool uses),
set ctx.final_message to the clarify question text, and never fall through
to relax/reframe or the google_search fallback.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import _execute_tool


def _make_ctx(message: str = "What's the timely filing deadline?") -> PipelineContext:
    ctx = PipelineContext(correlation_id="clarify-test", thread_id=None, message=message)
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = "copilot"
    ctx.thinking_chunks = []
    return ctx


def _clarify_dispatch(questions: list[str], status: str = "no_retrieval"):
    calls = []

    def fake_dispatch(call):
        calls.append(call)
        env = MagicMock()
        env.text = ""
        env.sources = []
        env.signal = "no_sources"
        env.extra = {
            "pipeline_trace": {
                "status": status, "n_chunks": 0,
                "dispatch_path": "a", "chosen_slot": None,
                "clarify_questions": questions,
            }
        }
        return env

    return calls, fake_dispatch


def test_single_clarify_question_sets_bypass_and_final_message():
    calls, fake_dispatch = _clarify_dispatch(["Which payer — Sunshine Health or Simply Healthcare?"])
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert ctx.react_bypass_integrate is True
    assert ctx.final_message == "Which payer — Sunshine Health or Simply Healthcare?"
    assert result["success"] is False
    assert result["clarify_questions"] == ["Which payer — Sunshine Health or Simply Healthcare?"]


def test_multiple_clarify_questions_joined_as_list():
    questions = ["Which payer?", "Which service date?"]
    calls, fake_dispatch = _clarify_dispatch(questions)
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert "Which payer?" in ctx.final_message
    assert "Which service date?" in ctx.final_message


def test_only_one_dispatch_call_no_retry_or_reframe():
    """The terminal bypass must fire on the FIRST call -- no relax/reframe
    round, no second network call, matching "don't attempt further tool
    calls" from the directive."""
    calls, fake_dispatch = _clarify_dispatch(["Which payer?"])
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert len(calls) == 1


def test_clears_sources_and_retrieval_signals():
    calls, fake_dispatch = _clarify_dispatch(["Which payer?"])
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert ctx.sources == []
    assert ctx.answer_set == {}


def test_no_retrieval_without_clarify_questions_does_not_bypass():
    """Regression guard: plain "nothing found" (empty clarify_questions)
    must NOT trigger the bypass -- it goes through the normal relax/reframe
    protocol exactly as before this change."""
    calls, fake_dispatch = _clarify_dispatch([], status="no_retrieval")
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert getattr(ctx, "react_bypass_integrate", False) is False


def test_clarify_questions_with_ok_status_does_not_bypass():
    """Guard: only status=no_retrieval triggers this -- a successful call
    that happens to carry a (stale/unexpected) clarify_questions value
    must not short-circuit a genuinely good answer."""
    calls, fake_dispatch = _clarify_dispatch(["Which payer?"], status="ok")
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert getattr(ctx, "react_bypass_integrate", False) is False
