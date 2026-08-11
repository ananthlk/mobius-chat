"""routing_keys.terminal_action == "clarify_low_confidence" call_number
escalation (2026-08-09, Chat Master directive, Retriever-confirmed live
contract).

Different signal from clarify_questions/no_retrieval (test_react_
clarify_questions.py): chunks CAME BACK -- this is not "nothing found" --
just under RAG's own confidence bar for the chosen slot. RAG's own
call_number turn-floor unlocks wider retrieval arms (c/d) at
call_number>=2 with a correspondingly higher confidence bar; react_loop.py
escalates call_number (same query, no citable_required/query changes) up
to 3 total attempts -- reusing the EXISTING 3-call ceiling rather than
stacking a second independent budget. After exhaustion, forces an honest
hedge (ctx.react_unfinished_reason="no_path_forward", same pattern as the
fast-mode thin-evidence hedge) rather than presenting under-confident
material as settled.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import _execute_tool


def _make_ctx(message: str = "Sunshine Health Florida urgent PA timeframe") -> PipelineContext:
    ctx = PipelineContext(correlation_id="lowconf-test", thread_id=None, message=message)
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = "copilot"
    ctx.thinking_chunks = []
    return ctx


_LONG_TEXT = "Real retrieved content about prior authorization timeframes. " * 3  # > 80 chars


def _env_for(
    terminal_action: str | None,
    *,
    text: str = _LONG_TEXT,
    adjusted_bar: float | None = None,
    chosen_slot_lb: float | None = None,
) -> MagicMock:
    env = MagicMock()
    env.text = text
    env.sources = []
    env.signal = "corpus_only"
    env.extra = {
        "pipeline_trace": {
            "status": "partial", "n_chunks": 3,
            "dispatch_path": "a", "chosen_slot": "direct_answer",
            "clarify_questions": [],
            "terminal_action": terminal_action,
            "routing_verdict_outcome": "partial_infeasible" if terminal_action else "satisfied",
            "routing_verdict_adjusted_bar": adjusted_bar,
            "chosen_slot_lb": chosen_slot_lb,
        }
    }
    return env


def _sequenced_dispatch(envs: list[MagicMock]):
    """Returns each env in sequence per dispatch() call; raises if called
    more times than envs provided (surfaces an unexpectedly-long loop)."""
    calls = []
    queue = list(envs)

    def fake_dispatch(call):
        calls.append(call)
        if not queue:
            raise AssertionError(f"dispatch() called more times ({len(calls)}) than scripted ({len(envs)})")
        return queue.pop(0)

    return calls, fake_dispatch


def test_first_dispatch_always_sends_call_number_1():
    calls, fake_dispatch = _sequenced_dispatch([_env_for(None)])
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert len(calls) == 1
    assert calls[0].inputs["call_number"] == 1


def test_no_escalation_when_first_call_already_confident():
    calls, fake_dispatch = _sequenced_dispatch([_env_for(None)])
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert len(calls) == 1
    assert getattr(ctx, "react_unfinished_reason", None) is None
    assert "confidence bar" not in result["result"]


def test_escalates_once_then_resolves():
    """call 1 low-confidence, call 2 clears the bar -- exactly 2 dispatch
    calls, call_number 1 then 2, no forced hedge."""
    calls, fake_dispatch = _sequenced_dispatch([
        _env_for("clarify_low_confidence"),
        _env_for(None),
    ])
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert len(calls) == 2
    assert calls[0].inputs["call_number"] == 1
    assert calls[1].inputs["call_number"] == 2
    assert getattr(ctx, "react_unfinished_reason", None) is None
    assert "confidence bar" not in result["result"]


def test_escalates_through_all_three_calls_then_hedges():
    """All 3 calls stay low-confidence -- exactly 3 dispatch calls
    (call_number 1, 2, 3), then a forced honest-hedge signal, no 4th
    call."""
    calls, fake_dispatch = _sequenced_dispatch([
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence", adjusted_bar=0.7225, chosen_slot_lb=0.7018),
    ])
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert len(calls) == 3
    assert [c.inputs["call_number"] for c in calls] == [1, 2, 3]
    assert ctx.react_unfinished_reason == "no_path_forward"
    assert "[Retrieval signal for reframe]" in result["result"]
    assert "confidence bar" in result["result"]
    assert "rule 1d" in result["result"]
    # Real numbers cited when routing_verdict supplies them.
    assert "0.70" in result["result"]
    assert "0.72" in result["result"]


def test_hedge_gracefully_omits_numbers_when_not_supplied():
    calls, fake_dispatch = _sequenced_dispatch([
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence"),  # no adjusted_bar/chosen_slot_lb
    ])
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert ctx.react_unfinished_reason == "no_path_forward"
    assert "confidence bar" in result["result"]
    assert "None" not in result["result"]  # no raw None leaking into user/model-facing text


def test_success_true_does_not_suppress_the_hedge():
    """A low-confidence result can still be >80 chars (content and
    confidence are different axes) -- the hedge must fire even when the
    generic `if not success:` gate wouldn't have caught it."""
    calls, fake_dispatch = _sequenced_dispatch([
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence"),
    ])
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert result["success"] is True  # >80 chars, real content
    assert ctx.react_unfinished_reason == "no_path_forward"  # hedge still forced


def test_transport_failure_mid_escalation_breaks_loop_without_crashing():
    calls, fake_dispatch = _sequenced_dispatch([_env_for("clarify_low_confidence")])

    def raising_second_call(call):
        calls.append(call)
        if len(calls) == 1:
            return _env_for("clarify_low_confidence")
        raise RuntimeError("simulated transport failure")

    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=raising_second_call):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)  # must not raise

    assert len(calls) == 2  # attempted the escalation, didn't retry past the failure
    assert result is not None


def test_query_and_citable_required_unchanged_across_escalation_calls():
    """This is a call_number-only escalation -- same slot query, no
    citable_required/query-text changes (a different axis from the
    existing relax/reframe protocol)."""
    calls, fake_dispatch = _sequenced_dispatch([
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence"),
        _env_for(None),
    ])
    ctx = _make_ctx()
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    queries = [c.inputs["query"] for c in calls]
    assert len(set(queries)) == 1, "escalation must reuse the exact same query, not reframe it"
