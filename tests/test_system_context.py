"""Tests for system_context support (2026-04-22).

Covers the end-to-end wiring:
  - ChatRequest accepts system_context and forwards it to the queue payload
  - Worker unpacks system_context from the payload and passes it to run_pipeline
  - run_pipeline stores it on PipelineContext.system_context
  - ReAct Round 0 short-circuits when the context answers the question
  - ReAct falls through to the normal tool loop on NEEDS_TOOLS
  - Missing/empty system_context preserves legacy behavior
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.api.chat import ChatRequest
from app.pipeline.context import PipelineContext
from app.pipeline.react.round0 import (
    ROUND0_SENTINEL,
    build_round0_user_message,
    build_round_context_prefix,
    try_system_context_round0,
)
# Back-compat aliases — the first-cut test file imported these from
# react_loop. Keep the aliased names so the test body reads the same.
from app.pipeline.react_loop import (
    _ROUND0_SENTINEL,
    _try_system_context_round0,
)
from app.services.doc_assembly import RETRIEVAL_SIGNAL_SYSTEM_CONTEXT


# ── ChatRequest model ─────────────────────────────────────────────────


def test_chat_request_accepts_system_context():
    req = ChatRequest(
        message="What was BHPF share in 2019?",
        system_context="bhpf_share_baseline: 0.16\nperiod: 2019",
    )
    assert req.system_context == "bhpf_share_baseline: 0.16\nperiod: 2019"


def test_chat_request_system_context_defaults_to_none():
    req = ChatRequest(message="hello")
    assert req.system_context is None


def test_chat_request_ignores_unknown_fields_and_keeps_system_context():
    """extra='ignore' shouldn't strip system_context (it's declared)."""
    req = ChatRequest(
        message="q",
        system_context="ctx",
        some_legacy_field="ignored",
    )
    assert req.system_context == "ctx"


# ── POST /chat → queue payload ────────────────────────────────────────


def test_post_chat_forwards_system_context_to_payload():
    """When body.system_context is set, payload should include it."""
    from app.api import chat as chat_module

    captured = {}

    class FakeQueue:
        def publish_request(self, cid, payload):
            captured["cid"] = cid
            captured["payload"] = payload

    with patch.object(chat_module, "get_queue", return_value=FakeQueue()), \
         patch.object(chat_module, "ensure_thread", return_value="t-123"):
        body = ChatRequest(message="q", system_context="verified: 42")
        resp = chat_module.post_chat(body, user_id=None)

    assert resp.thread_id == "t-123"
    assert captured["payload"]["message"] == "q"
    assert captured["payload"]["system_context"] == "verified: 42"


def test_post_chat_omits_system_context_when_none():
    from app.api import chat as chat_module

    captured = {}

    class FakeQueue:
        def publish_request(self, cid, payload):
            captured["payload"] = payload

    with patch.object(chat_module, "get_queue", return_value=FakeQueue()), \
         patch.object(chat_module, "ensure_thread", return_value="t-1"):
        body = ChatRequest(message="q")
        chat_module.post_chat(body, user_id=None)

    assert "system_context" not in captured["payload"]


def test_post_chat_omits_empty_system_context():
    """Empty string is falsy — should be omitted from payload."""
    from app.api import chat as chat_module

    captured = {}

    class FakeQueue:
        def publish_request(self, cid, payload):
            captured["payload"] = payload

    with patch.object(chat_module, "get_queue", return_value=FakeQueue()), \
         patch.object(chat_module, "ensure_thread", return_value="t-1"):
        body = ChatRequest(message="q", system_context="")
        chat_module.post_chat(body, user_id=None)

    assert "system_context" not in captured["payload"]


# ── Worker unpacking ──────────────────────────────────────────────────


def test_worker_passes_system_context_to_run_pipeline():
    from app.worker import run as worker_run

    captured_kwargs = {}

    def fake_run_pipeline(cid, msg, tid, **kwargs):
        captured_kwargs.update(kwargs)

    # Monkey-patch the imported symbol inside process_one.
    with patch("app.pipeline.orchestrator.run_pipeline", side_effect=fake_run_pipeline):
        worker_run.process_one(
            "cid-1",
            {
                "message": "q",
                "thread_id": "t-1",
                "system_context": "verified: 42",
            },
        )

    assert captured_kwargs.get("system_context") == "verified: 42"


def test_worker_normalizes_non_string_system_context_to_none():
    from app.worker import run as worker_run

    captured_kwargs = {}

    def fake_run_pipeline(cid, msg, tid, **kwargs):
        captured_kwargs.update(kwargs)

    with patch("app.pipeline.orchestrator.run_pipeline", side_effect=fake_run_pipeline):
        worker_run.process_one(
            "cid-2",
            {"message": "q", "thread_id": None, "system_context": {"not": "a string"}},
        )

    assert captured_kwargs.get("system_context") is None


def test_worker_normalizes_whitespace_system_context_to_none():
    from app.worker import run as worker_run

    captured_kwargs = {}

    def fake_run_pipeline(cid, msg, tid, **kwargs):
        captured_kwargs.update(kwargs)

    with patch("app.pipeline.orchestrator.run_pipeline", side_effect=fake_run_pipeline):
        worker_run.process_one(
            "cid-3",
            {"message": "q", "thread_id": None, "system_context": "   \n  "},
        )

    assert captured_kwargs.get("system_context") is None


# ── PipelineContext ───────────────────────────────────────────────────


def test_pipeline_context_has_system_context_field():
    ctx = PipelineContext(
        correlation_id="c",
        thread_id=None,
        message="hi",
        system_context="verified: 1",
    )
    assert ctx.system_context == "verified: 1"


def test_pipeline_context_system_context_defaults_to_none():
    ctx = PipelineContext(correlation_id="c", thread_id=None, message="hi")
    assert ctx.system_context is None


# ── Round 0 short-circuit ─────────────────────────────────────────────


def _mk_ctx(sys_ctx: str | None, message: str = "What is BHPF share in 2019?") -> PipelineContext:
    ctx = PipelineContext(
        correlation_id="cid",
        thread_id="tid",
        message=message,
        system_context=sys_ctx,
    )
    ctx.effective_message = message
    return ctx


def test_round0_returns_false_when_no_system_context():
    ctx = _mk_ctx(None)
    assert _try_system_context_round0(ctx, emitter=None) is False


def test_round0_returns_false_on_empty_system_context():
    ctx = _mk_ctx("")
    assert _try_system_context_round0(ctx, emitter=None) is False


def test_round0_returns_false_when_no_question():
    ctx = _mk_ctx("some: data", message="")
    ctx.effective_message = ""
    assert _try_system_context_round0(ctx, emitter=None) is False


def test_round0_short_circuits_when_context_sufficient():
    """LLM returns a direct answer → finalize and return True."""
    ctx = _mk_ctx("bhpf_share_baseline: 0.16")
    emitter = MagicMock()

    with patch(
        "app.pipeline.react.prompts._call_llm_json",
        return_value="BHPF's 2019 baseline share was 16%.",
    ):
        result = _try_system_context_round0(ctx, emitter=emitter)

    assert result is True
    assert ctx.final_message == "BHPF's 2019 baseline share was 16%."
    assert ctx.react_last_tool == "system_context"
    assert ctx.react_rounds_used == 0  # short-circuit path
    assert ctx.sources == []
    # Rec 2: new retrieval signal for analytics clarity.
    assert ctx.retrieval_signals == [RETRIEVAL_SIGNAL_SYSTEM_CONTEXT]


def test_round0_falls_through_on_needs_tools_sentinel():
    """Bare NEEDS_TOOLS → return False, no finalize."""
    ctx = _mk_ctx("bhpf_share_baseline: 0.16")

    with patch(
        "app.pipeline.react.prompts._call_llm_json",
        return_value=_ROUND0_SENTINEL,
    ):
        result = _try_system_context_round0(ctx, emitter=None)

    assert result is False
    assert ctx.final_message == ""  # not finalized


def test_round0_falls_through_on_needs_tools_with_reason():
    """'NEEDS_TOOLS: …' prefix → also falls through."""
    ctx = _mk_ctx("bhpf_share_baseline: 0.16")

    with patch(
        "app.pipeline.react.prompts._call_llm_json",
        return_value="NEEDS_TOOLS: 2020 values not in context",
    ):
        result = _try_system_context_round0(ctx, emitter=None)

    assert result is False


def test_round0_falls_through_when_llm_raises():
    """Defensive: any LLM exception → fall through to normal loop."""
    ctx = _mk_ctx("bhpf_share_baseline: 0.16")

    with patch(
        "app.pipeline.react.prompts._call_llm_json",
        side_effect=RuntimeError("provider hiccup"),
    ):
        result = _try_system_context_round0(ctx, emitter=None)

    assert result is False


def test_round0_falls_through_on_empty_llm_response():
    ctx = _mk_ctx("verified: 42")
    with patch("app.pipeline.react.prompts._call_llm_json", return_value=""):
        assert _try_system_context_round0(ctx, emitter=None) is False


def test_round0_emits_thinking_lines_on_success():
    """UI-visible lines should fire even on the short-circuit path."""
    ctx = _mk_ctx("k: v")
    emits: list[str] = []

    def emitter(msg):
        emits.append(str(msg))

    with patch(
        "app.pipeline.react.prompts._call_llm_json",
        return_value="Answer from context.",
    ):
        _try_system_context_round0(ctx, emitter=emitter)

    joined = " ".join(emits)
    assert "pre-loaded context" in joined.lower() or "pre-loaded" in joined.lower()


def test_round0_prompt_passes_context_and_question_to_llm():
    """Sanity: the LLM user message must contain both the context and the question."""
    ctx = _mk_ctx("bhpf_share_baseline: 0.16", message="What was the share?")
    captured = {}

    def fake_llm(system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        return "16%"

    with patch("app.pipeline.react.prompts._call_llm_json", side_effect=fake_llm):
        _try_system_context_round0(ctx, emitter=None)

    assert "bhpf_share_baseline" in captured["user"]
    assert "What was the share?" in captured["user"]
    assert "SYSTEM CONTEXT" in captured["user"]
    assert "NEEDS_TOOLS" in captured["system"]


# ── Round 0 helpers (pure functions, no LLM) ──────────────────────────


def test_build_round0_user_message_contains_context_and_question():
    msg = build_round0_user_message("k: v", "why?")
    assert "k: v" in msg
    assert "why?" in msg
    assert "SYSTEM CONTEXT" in msg
    assert "END SYSTEM CONTEXT" in msg


def test_build_round_context_prefix_is_prependable():
    prefix = build_round_context_prefix("k: v")
    assert prefix.startswith("[SYSTEM CONTEXT")
    assert "k: v" in prefix
    # Must end with blank line so caller can string-concat cleanly.
    assert prefix.endswith("\n\n")


def test_round0_sentinel_value():
    """Contract: the sentinel is the literal string 'NEEDS_TOOLS'."""
    assert ROUND0_SENTINEL == "NEEDS_TOOLS"
    assert _ROUND0_SENTINEL == "NEEDS_TOOLS"  # back-compat alias


# ── Round 0: new module API + DI ──────────────────────────────────────


def test_try_system_context_round0_with_injected_llm():
    """round0.try_system_context_round0 accepts llm_caller/finalizer for DI."""
    ctx = _mk_ctx("k: v")

    def fake_llm(system, user, **_):
        return "direct answer"

    calls: list[tuple] = []

    def fake_finalizer(ctx_in, answer, sources, signal, last_tool, emitter):
        calls.append((answer, signal, last_tool))
        ctx_in.final_message = answer
        ctx_in.retrieval_signals = [signal]
        ctx_in.react_last_tool = last_tool

    ok = try_system_context_round0(
        ctx, emitter=None, llm_caller=fake_llm, finalizer=fake_finalizer,
    )
    assert ok is True
    assert calls == [("direct answer", RETRIEVAL_SIGNAL_SYSTEM_CONTEXT, "system_context")]
    assert ctx.react_rounds_used == 0


# ── Orchestrator response envelope: answered_from_system_context ──────


def test_publish_completed_adds_answered_from_system_context_flag():
    """When retrieval_signals contains system_context, client payload
    should carry ``answered_from_system_context: True``."""
    from app.pipeline import orchestrator as orch

    ctx = PipelineContext(correlation_id="cid", thread_id=None, message="q")
    ctx.final_message = "42"
    ctx.retrieval_signals = [RETRIEVAL_SIGNAL_SYSTEM_CONTEXT]
    ctx.response_payload = {
        "correlation_id": "cid",
        "status": "completed",
        "message": "42",
        "sources": [],
    }

    captured = {}

    class FakeQueue:
        def publish_response(self, cid, payload):
            captured["payload"] = payload

    class FakePersistence:
        def save_turn_with_messages(self, **_):
            pass
        def save_turn(self, **_):
            pass

    with patch.object(orch, "get_queue", return_value=FakeQueue()), \
         patch.object(orch, "get_persistence", return_value=FakePersistence()), \
         patch.object(orch, "clear_progress"), \
         patch.object(orch, "save_state_full"), \
         patch("app.services.task_manager_promotion.promote"):
        orch._publish_completed(ctx, t0_start=0.0)

    assert captured["payload"]["answered_from_system_context"] is True


def test_publish_completed_omits_flag_when_signal_absent():
    """Normal RAG / web turn should NOT have the flag."""
    from app.pipeline import orchestrator as orch
    from app.services.doc_assembly import RETRIEVAL_SIGNAL_CORPUS_ONLY

    ctx = PipelineContext(correlation_id="cid", thread_id=None, message="q")
    ctx.final_message = "normal answer"
    ctx.retrieval_signals = [RETRIEVAL_SIGNAL_CORPUS_ONLY]
    ctx.response_payload = {"correlation_id": "cid", "status": "completed", "message": "x"}

    captured = {}

    class FakeQueue:
        def publish_response(self, cid, payload):
            captured["payload"] = payload

    class FakePersistence:
        def save_turn_with_messages(self, **_):
            pass
        def save_turn(self, **_):
            pass

    with patch.object(orch, "get_queue", return_value=FakeQueue()), \
         patch.object(orch, "get_persistence", return_value=FakePersistence()), \
         patch.object(orch, "clear_progress"), \
         patch.object(orch, "save_state_full"), \
         patch("app.services.task_manager_promotion.promote"):
        orch._publish_completed(ctx, t0_start=0.0)

    assert "answered_from_system_context" not in captured["payload"]
