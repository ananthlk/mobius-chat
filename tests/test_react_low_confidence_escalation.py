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
    module_trace: list | None = None,
    latency_ms: dict | None = None,
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
            "module_trace": module_trace,
            "latency_ms": latency_ms,
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
    call. Chat Master clarification (2026-08-09): the exhaustion answer is
    produced by a DEDICATED synthesis pass (bypassing the model's own next
    round entirely, same distrust-marginal-self-report rationale as the
    fast-mode thin-evidence path), not a reframe-signal instruction hoping
    the model complies. is_terminal=True skips the model's decision for
    this round but still routes through the normal finalize/integrator
    pipeline (real sources preserved, not react_bypass_integrate's
    plain-message short-circuit)."""
    calls, fake_dispatch = _sequenced_dispatch([
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence", adjusted_bar=0.7225, chosen_slot_lb=0.7018),
    ])
    ctx = _make_ctx()

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        return "Prior auth timeframes are unclear from what's available -- best guidance found says 72 hours for urgent requests, but this isn't confirmed."

    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert len(calls) == 3
    assert [c.inputs["call_number"] for c in calls] == [1, 2, 3]
    assert ctx.react_unfinished_reason == "no_path_forward"
    assert result["is_terminal"] is True
    assert result["success"] is True
    assert result["sources"] == []  # matches this test's _env_for (no sources), still wired through
    assert "72 hours" in result["result"]
    assert "Think mode" in result["result"]


def test_exhaustion_falls_back_to_code_hedge_when_synthesis_fails():
    """Zero-fabrication-risk fallback: if the dedicated synthesis call
    itself fails or returns nothing usable, ship the pure code-constructed
    excerpt hedge -- never an LLM call that could itself hallucinate past
    what's literally in the low-confidence evidence."""
    calls, fake_dispatch = _sequenced_dispatch([
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence"),
    ])
    ctx = _make_ctx()

    def failing_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        raise RuntimeError("simulated provider failure")

    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=failing_llm):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert ctx.react_unfinished_reason == "no_path_forward"
    assert result["is_terminal"] is True
    assert result["result"]  # code-constructed hedge, non-empty
    assert "None" not in result["result"]  # no raw None leaking into user-facing text


def test_exhaustion_is_terminal_skips_next_round_but_not_the_integrator():
    """Distinguishes this from react_bypass_integrate (clarify_questions/
    refuse): is_terminal routes through the normal finalize pipeline, so
    real (if under-confident) sources survive as citations rather than
    being discarded like the plain-message bypass path."""
    calls, fake_dispatch = _sequenced_dispatch([
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence"),
        _env_for("clarify_low_confidence"),
    ])
    ctx = _make_ctx()

    def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
        return "Best-effort honest answer from limited evidence."

    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch), \
         patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert result["is_terminal"] is True
    assert getattr(ctx, "react_bypass_integrate", False) is False  # NOT the clarify_questions mechanism


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


class TestRagCallRoundsDiagnostics:
    """2026-08-09, Chat Master directive, add-on to #80: ctx._rag_call_rounds
    accumulates one entry per ACTUAL RAG HTTP call this turn (not per
    search_corpus tool invocation) so Chat FE can render collapsible
    "Round 1/2/3" blocks in the Diagnostics panel. Separate accumulator
    from ctx._rag_call_history (load-bearing for relax/reframe decisions) --
    purely additive, must never affect the escalation logic itself."""

    def test_single_call_produces_one_round_record(self):
        calls, fake_dispatch = _sequenced_dispatch([_env_for(None, module_trace=[{"stage": "route"}], latency_ms={"total_ms": 420})])
        ctx = _make_ctx()
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
            _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

        rounds = ctx._rag_call_rounds
        assert len(rounds) == 1
        assert rounds[0]["round_n"] == 1
        assert rounds[0]["query"] == ctx.message
        assert rounds[0]["module_trace"] == [{"stage": "route"}]
        assert rounds[0]["latency_ms"] == {"total_ms": 420}
        assert rounds[0]["terminal_action"] is None

    def test_escalation_produces_one_record_per_actual_call(self):
        calls, fake_dispatch = _sequenced_dispatch([
            _env_for("clarify_low_confidence", latency_ms={"total_ms": 300}),
            _env_for("clarify_low_confidence", latency_ms={"total_ms": 450}),
            _env_for(None, latency_ms={"total_ms": 600}),
        ])
        ctx = _make_ctx()
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
            _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

        rounds = ctx._rag_call_rounds
        assert len(rounds) == 3
        assert [r["round_n"] for r in rounds] == [1, 2, 3]
        assert [r["terminal_action"] for r in rounds] == ["clarify_low_confidence", "clarify_low_confidence", None]
        assert [r["latency_ms"]["total_ms"] for r in rounds] == [300, 450, 600]

    def test_rag_call_history_unaffected_by_the_new_accumulator(self):
        """The existing, load-bearing accumulator must keep its own
        pre-existing shape and count -- this is a genuinely separate list,
        not a rename."""
        calls, fake_dispatch = _sequenced_dispatch([
            _env_for("clarify_low_confidence"),
            _env_for(None),
        ])
        ctx = _make_ctx()
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
            _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

        assert len(ctx._rag_call_history) == 1  # one summary entry per TOOL invocation
        assert len(ctx._rag_call_rounds) == 2   # two entries per ACTUAL rag call
        assert "round_n" not in ctx._rag_call_history[0]
        assert "module_trace" not in ctx._rag_call_history[0]


class TestMultiEntitySlotPriority:
    """2026-08-12, Chat Master directive -- live incident: a 3-way payer
    comparison burned its whole 3-call budget escalating/retrying on one
    entity before a later entity ever got a first search. A retry on
    entity 1 must never consume entity 2's unspent slot. Gating rule
    (see _execute_tool's open_gaps docstring): retries (escalation or
    clarify-fallback) only fire when len(open_gaps) <= 1 -- 0 (round 1,
    no evidence_review yet) or 1 (this IS the last outstanding gap, safe
    to spend extra budget) allow it; 2+ (another gap has had zero
    attempts) blocks it."""

    def test_escalation_blocked_when_multiple_gaps_still_open(self):
        calls, fake_dispatch = _sequenced_dispatch([_env_for("clarify_low_confidence")])
        ctx = _make_ctx()
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
            result = _execute_tool(
                "rag", {"query": ctx.message}, ctx, emitter=None,
                open_gaps=["Aetna timely filing deadline", "Molina timely filing deadline"],
            )

        assert len(calls) == 1  # no escalation calls fired
        assert result.get("is_terminal") is not True  # not the exhaustion path either
        assert getattr(ctx, "react_unfinished_reason", None) is None

    def test_escalation_allowed_when_zero_gaps_open(self):
        """Round 1: no evidence_review has run yet, open_gaps is empty --
        always allowed, matches 'slot 1 always available'."""
        calls, fake_dispatch = _sequenced_dispatch([
            _env_for("clarify_low_confidence"),
            _env_for("clarify_low_confidence"),
            _env_for("clarify_low_confidence"),
        ])
        ctx = _make_ctx()

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            return "Best-effort honest answer."

        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None, open_gaps=[])

        assert len(calls) == 3  # full escalation ran

    def test_escalation_allowed_when_exactly_one_gap_open(self):
        """This IS the last outstanding gap -- nothing else is waiting on
        the budget, so it's safe to spend extra calls perfecting it."""
        calls, fake_dispatch = _sequenced_dispatch([
            _env_for("clarify_low_confidence"),
            _env_for("clarify_low_confidence"),
            _env_for("clarify_low_confidence"),
        ])
        ctx = _make_ctx()

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            return "Best-effort honest answer."

        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            _execute_tool(
                "rag", {"query": ctx.message}, ctx, emitter=None,
                open_gaps=["Molina timely filing deadline"],
            )

        assert len(calls) == 3  # full escalation ran, only one gap so nothing else waiting

    def test_gap_blocked_result_still_usable_not_a_dead_end(self):
        """Blocking escalation isn't a failure state -- the call's own
        result (even if low-confidence) must still flow through normally
        so the round loop can move on to the next gap."""
        calls, fake_dispatch = _sequenced_dispatch([_env_for("clarify_low_confidence", text=_LONG_TEXT)])
        ctx = _make_ctx()
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
            result = _execute_tool(
                "rag", {"query": ctx.message}, ctx, emitter=None,
                open_gaps=["Aetna", "Molina"],
            )

        assert result["success"] is True  # >80 chars, real content -- not thrown away
        assert result["sources"] == []

    def test_clarify_fallback_blocked_when_multiple_gaps_still_open(self):
        """Same gate applied to the OTHER automatic same-round retry
        (clarify_questions advisory fallback, not just low-confidence
        escalation) -- both are same-round budget spends that must
        respect entity priority."""
        def fake_dispatch(call):
            env = MagicMock()
            env.text = ""
            env.sources = []
            env.signal = "no_sources"
            env.extra = {
                "pipeline_trace": {
                    "status": "no_retrieval", "n_chunks": 0,
                    "dispatch_path": "a", "chosen_slot": None,
                    "clarify_questions": ["What service type?"],
                    "terminal_action": None,
                }
            }
            return env

        ctx = _make_ctx()
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch) as mock_dispatch:
            _execute_tool(
                "rag", {"query": ctx.message}, ctx, emitter=None,
                open_gaps=["Aetna timely filing deadline", "Molina timely filing deadline"],
            )

        assert mock_dispatch.call_count == 1  # no fallback retry fired

    def test_default_open_gaps_none_never_blocks(self):
        """Existing callers that don't pass open_gaps at all (default
        None) must see zero behavior change -- regression guard for every
        pre-#85 test in this file and elsewhere."""
        calls, fake_dispatch = _sequenced_dispatch([
            _env_for("clarify_low_confidence"),
            _env_for("clarify_low_confidence"),
            _env_for("clarify_low_confidence"),
        ])
        ctx = _make_ctx()

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            return "Best-effort honest answer."

        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)  # no open_gaps kwarg

        assert len(calls) == 3
