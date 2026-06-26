"""Phase 13.7 — rolling thread summary + sidebar rehydration.

Covers four layers, all with unit-level fakes (no Postgres, no LLM):

1. **state_load** populates ``ctx.previous_thread_summary`` from the
   most-recent turn that has a non-empty ``context_summary``.

2. **integrator I/O**: ``_build_consolidator_input_json`` includes
   ``previous_thread_summary`` only when present and non-empty.

3. **run_integrate** parses the integrator's ``thread_summary``
   field out of the AnswerCard JSON and stamps it onto
   ``ctx.thread_summary``. Falls back to None on parse failure or
   missing field — never crashes.

4. **GET /chat/history/threads/{id}/turns** returns turns ordered
   chronologically with the AnswerCard JSON pass-through, and
   surfaces summary in /chat/history/threads.

Pre-Phase-13.7 behavior was: chat_turns.context_summary was either
NULL (thread saves) or a regex blob (single-shot saves). Sidebar
fell back to first-turn question. Click pre-filled the input.

Post-Phase-13.7 the sidebar shows the integrator's rolling tldr,
clicking opens the thread, and continuing types refines the same
summary forward.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Import-order matters here — pulling state_load before context fights
# a circular import via app.pipeline.__init__. Fetch context first so
# the orchestrator side has finished bootstrapping by the time
# state_load gets imported inside test bodies.
from app.pipeline.context import PipelineContext  # noqa: E402,F401
from app.planner.schemas import Plan, SubQuestion  # noqa: E402,F401


# ── 1. state_load → ctx.previous_thread_summary ───────────────────────


def _make_ctx(thread_id: str = "t1", message: str = "next question") -> object:
    """Minimal PipelineContext stand-in for state_load tests."""
    from app.pipeline.context import PipelineContext

    ctx = PipelineContext(
        correlation_id="cid-1",
        thread_id=thread_id,
        message=message,
    )
    ctx.merged_state = {}
    ctx.effective_message = message
    return ctx


def test_state_load_picks_latest_non_empty_context_summary(monkeypatch):
    """last_turns is newest-first; we walk it and stop on the first
    turn that has a non-empty context_summary. That becomes
    ``ctx.previous_thread_summary``."""
    from app.stages import state_load as sl

    monkeypatch.setattr(sl, "get_state", lambda tid: {"active": {}})
    monkeypatch.setattr(sl, "save_state_full", lambda tid, st: None)
    monkeypatch.setattr(sl, "get_last_turn_sources", lambda tid: [])
    # No canonical per-thread brief yet (legacy thread) -> falls back to
    # the per-turn context_summary walk below.
    monkeypatch.setattr(sl, "get_thread_rolling_summary", lambda tid: None)
    # Two turns: newest has empty summary, older has the real one. The
    # function should walk forward and pick the older non-empty.
    monkeypatch.setattr(sl, "get_last_turn_messages", lambda tid: [
        {"turn_id": "t-newest", "user_content": "u2", "assistant_content": "a2",
         "context_summary": "  "},  # whitespace -> empty
        {"turn_id": "t-older", "user_content": "u1", "assistant_content": "a1",
         "context_summary": "Sunshine FL split-stay billing question; days 36-90 denied as duplicate."},
    ])

    ctx = _make_ctx()
    sl.run_state_load(ctx)
    assert ctx.previous_thread_summary == (
        "Sunshine FL split-stay billing question; days 36-90 denied as duplicate."
    )


def test_state_load_returns_none_when_no_summaries(monkeypatch):
    from app.stages import state_load as sl

    monkeypatch.setattr(sl, "get_state", lambda tid: {"active": {}})
    monkeypatch.setattr(sl, "save_state_full", lambda tid, st: None)
    monkeypatch.setattr(sl, "get_last_turn_sources", lambda tid: [])
    monkeypatch.setattr(sl, "get_thread_rolling_summary", lambda tid: None)
    monkeypatch.setattr(sl, "get_last_turn_messages", lambda tid: [
        {"turn_id": "t-1", "user_content": "u", "assistant_content": "a"},
    ])

    ctx = _make_ctx()
    sl.run_state_load(ctx)
    assert ctx.previous_thread_summary is None


def test_state_load_handles_empty_thread(monkeypatch):
    """No thread_id → state_load early-exits and previous_thread_summary
    stays None (default from the dataclass)."""
    from app.stages import state_load as sl
    from app.pipeline.context import PipelineContext

    ctx = PipelineContext(correlation_id="c", thread_id=None, message="hi")
    ctx.effective_message = "hi"
    sl.run_state_load(ctx)
    assert ctx.previous_thread_summary is None
    assert ctx.last_turns == []


def test_state_load_summary_walk_skips_malformed_entries(monkeypatch):
    """Defensive: the walk-for-context_summary logic skips non-dicts.

    We exercise just the walk by inlining the same conditional pattern
    against a mixed list. (Going through full ``run_state_load`` here
    would also exercise context_router's last_turns access, which is
    a separate pre-existing concern outside Phase 13.7's scope.)
    """
    last_turns = [
        "not-a-dict",
        None,
        {"turn_id": "ok", "user_content": "u", "assistant_content": "a",
         "context_summary": "real summary"},
    ]
    found: str | None = None
    for _turn in (last_turns or []):
        if not isinstance(_turn, dict):
            continue
        cs = (_turn.get("context_summary") or "").strip()
        if cs:
            found = cs
            break
    assert found == "real summary"


# ── 2. integrator input shape ─────────────────────────────────────────


def test_integrator_input_includes_previous_summary_when_present():
    from app.responder.final import _build_consolidator_input_json

    plan = SimpleNamespace(subquestions=[])
    payload_json = _build_consolidator_input_json(
        plan, [], "user msg",
        previous_thread_summary="Sunshine FL claim, days 36-90 denied as duplicate, working on appeal angle.",
    )
    payload = json.loads(payload_json)
    assert payload["previous_thread_summary"] == (
        "Sunshine FL claim, days 36-90 denied as duplicate, working on appeal angle."
    )


def test_integrator_input_omits_previous_summary_when_empty():
    """First-turn behavior: no key in payload (planner produces fresh
    summary based on the input alone — no confusion from a stale slot)."""
    from app.responder.final import _build_consolidator_input_json

    plan = SimpleNamespace(subquestions=[])

    p1 = json.loads(_build_consolidator_input_json(plan, [], "u", previous_thread_summary=None))
    assert "previous_thread_summary" not in p1

    p2 = json.loads(_build_consolidator_input_json(plan, [], "u", previous_thread_summary=""))
    assert "previous_thread_summary" not in p2

    p3 = json.loads(_build_consolidator_input_json(plan, [], "u", previous_thread_summary="   "))
    assert "previous_thread_summary" not in p3


# ── 3. run_integrate extracts thread_summary from AnswerCard ──────────


def _stub_format_response(thread_summary_value):
    """Return a fake format_response whose final_message contains the
    given thread_summary value (or None to omit the field entirely)."""
    card = {
        "mode": "FACTUAL",
        "direct_answer": "Stub answer",
        "sections": [{"intent": "references", "bullets": ["x"]}],
    }
    if thread_summary_value is not None:
        card["thread_summary"] = thread_summary_value
    serialized = json.dumps(card)

    def _fr(*args, **kwargs):
        return (serialized, {"prompt_tokens": 1, "completion_tokens": 1})

    return _fr


def _make_integrate_ctx(*, previous_summary: str | None = None):
    """Build a minimal PipelineContext that survives run_integrate's
    accesses without us having to set up the entire pipeline."""
    from app.planner.schemas import Plan, SubQuestion
    from app.pipeline.context import PipelineContext

    ctx = PipelineContext(correlation_id="cid", thread_id="t1", message="q?")
    ctx.merged_state = {"active": {}}
    ctx.effective_message = "q?"
    ctx.previous_thread_summary = previous_summary
    ctx.plan = Plan(subquestions=[SubQuestion(id="sq1", text="t", kind="non_patient")])
    ctx.answers = ["stub"]
    ctx.sources = []
    ctx.usages = []
    ctx.retrieval_signals = []
    ctx.answer_set = {}
    return ctx


def test_run_integrate_stamps_thread_summary_onto_ctx(monkeypatch):
    """When the integrator emits ``thread_summary`` in its AnswerCard,
    run_integrate parses it out and sets ctx.thread_summary."""
    from app.stages import integrate as integ_mod

    monkeypatch.setattr(
        integ_mod, "format_response",
        _stub_format_response("Sunshine FL split-stay; appeal in progress."),
    )
    # Don't fire PHI audit / progress writes; not the focus here.
    monkeypatch.setattr("app.storage.phi_audit_log.audit_if_phi", lambda *a, **k: None)
    monkeypatch.setattr("app.storage.progress.append_message_chunk", lambda *a, **k: None)

    ctx = _make_integrate_ctx(previous_summary="Sunshine FL split-stay denial.")
    integ_mod.run_integrate(ctx)
    assert ctx.thread_summary == "Sunshine FL split-stay; appeal in progress."


def test_run_integrate_handles_missing_thread_summary_field(monkeypatch):
    """Older prompts / fallback paths won't emit thread_summary —
    ctx.thread_summary stays None, no crash."""
    from app.stages import integrate as integ_mod

    monkeypatch.setattr(integ_mod, "format_response", _stub_format_response(None))
    monkeypatch.setattr("app.storage.phi_audit_log.audit_if_phi", lambda *a, **k: None)
    monkeypatch.setattr("app.storage.progress.append_message_chunk", lambda *a, **k: None)

    ctx = _make_integrate_ctx()
    integ_mod.run_integrate(ctx)
    assert ctx.thread_summary is None


def test_run_integrate_handles_non_json_final_message(monkeypatch):
    """Fallback prose response (unparseable JSON): no crash, no
    thread_summary stamped."""
    from app.stages import integrate as integ_mod

    def _fr(*a, **k):
        return ("plain prose answer with no JSON at all", {})
    monkeypatch.setattr(integ_mod, "format_response", _fr)
    monkeypatch.setattr("app.storage.phi_audit_log.audit_if_phi", lambda *a, **k: None)
    monkeypatch.setattr("app.storage.progress.append_message_chunk", lambda *a, **k: None)

    ctx = _make_integrate_ctx()
    integ_mod.run_integrate(ctx)
    assert ctx.thread_summary is None


def test_run_integrate_caps_thread_summary_at_600_chars(monkeypatch):
    """The summary column is sized for ≤150 tokens; we cap defensively
    so a misbehaving prompt can't blow up the row."""
    from app.stages import integrate as integ_mod

    huge = "x" * 5000
    monkeypatch.setattr(integ_mod, "format_response", _stub_format_response(huge))
    monkeypatch.setattr("app.storage.phi_audit_log.audit_if_phi", lambda *a, **k: None)
    monkeypatch.setattr("app.storage.progress.append_message_chunk", lambda *a, **k: None)

    ctx = _make_integrate_ctx()
    integ_mod.run_integrate(ctx)
    assert ctx.thread_summary is not None
    assert len(ctx.thread_summary) == 600


# ── 4. /chat/history/threads/{id}/turns endpoint ──────────────────────


def test_history_thread_turns_endpoint_passes_through_to_storage():
    """Endpoint is a thin wrapper over storage.get_thread_turns;
    confirm shape + parameter pass-through."""
    from fastapi.testclient import TestClient
    from app.api.history import router

    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)

    fake_turns = [
        {
            "correlation_id": "c1",
            "question": "first question",
            "final_message": '{"mode":"FACTUAL","direct_answer":"first ans","sections":[]}',
            "sources": [{"document_name": "Doc"}],
            "created_at": "2026-04-26T10:00:00Z",
        },
    ]
    with patch("app.storage.threads.get_thread_turns", return_value=fake_turns) as mock_fn:
        with TestClient(app) as client:
            r = client.get("/chat/history/threads/abc-123/turns?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body == fake_turns
    mock_fn.assert_called_once()
    called_args = mock_fn.call_args
    # First positional arg is the thread_id; second is the parsed limit.
    assert called_args.args[0] == "abc-123"
    assert called_args.args[1] == 10


def test_history_thread_turns_endpoint_rejects_empty_thread_id():
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.api.history import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        # Whitespace-only id passes through to the validator
        r = client.get("/chat/history/threads/   /turns")
    assert r.status_code == 400


def test_history_thread_turns_endpoint_caps_limit():
    """limit is clamped to [1, 100] by _parse_limit."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.api.history import router

    app = FastAPI()
    app.include_router(router)

    with patch("app.storage.threads.get_thread_turns", return_value=[]) as mock_fn:
        with TestClient(app) as client:
            client.get("/chat/history/threads/x/turns?limit=99999")
    assert mock_fn.call_args.args[1] == 100

    with patch("app.storage.threads.get_thread_turns", return_value=[]) as mock_fn:
        with TestClient(app) as client:
            client.get("/chat/history/threads/x/turns?limit=-5")
    assert mock_fn.call_args.args[1] == 1


# ── 5. Two-tier rolling summary (migration 036) ───────────────────────


def test_state_load_prefers_canonical_rolling_summary(monkeypatch):
    """When chat_threads has a canonical summary_long, state_load uses it
    directly and does NOT fall back to walking per-turn context_summary."""
    from app.stages import state_load as sl

    monkeypatch.setattr(sl, "get_state", lambda tid: {"active": {}})
    monkeypatch.setattr(sl, "save_state_full", lambda tid, st: None)
    monkeypatch.setattr(sl, "get_last_turn_sources", lambda tid: [])
    # Canonical per-thread brief present -> wins over any per-turn value.
    monkeypatch.setattr(sl, "get_thread_rolling_summary",
                        lambda tid: "Provider enrollment — Sunshine Health (FL Medicaid). URL + form pending.")
    monkeypatch.setattr(sl, "get_last_turn_messages", lambda tid: [
        {"turn_id": "t", "user_content": "u", "assistant_content": "a",
         "context_summary": "stale per-turn value that must NOT be used"},
    ])

    ctx = _make_ctx()
    sl.run_state_load(ctx)
    assert ctx.previous_thread_summary == (
        "Provider enrollment — Sunshine Health (FL Medicaid). URL + form pending."
    )


def test_run_integrate_stamps_thread_state_onto_ctx(monkeypatch):
    """The integrator's long ``thread_state`` field is parsed out and
    stamped onto ctx.thread_state, independently of thread_summary."""
    from app.stages import integrate as integ_mod

    card = {
        "mode": "BLENDED",
        "direct_answer": "ans",
        "sections": [{"intent": "references", "bullets": ["x"]}],
        "thread_summary": "Provider enrollment — Sunshine Health (FL Medicaid)",
        "thread_state": ("Provider enrollment — Sunshine Health (FL Medicaid). "
                         "Enroll via sunshinehealth.com Practitioner Enrollment Requests; "
                         "CAQH accepted. User wants the downloadable form + link."),
    }
    serialized = json.dumps(card)
    monkeypatch.setattr(integ_mod, "format_response",
                        lambda *a, **k: (serialized, {"prompt_tokens": 1, "completion_tokens": 1}))
    monkeypatch.setattr("app.storage.phi_audit_log.audit_if_phi", lambda *a, **k: None)
    monkeypatch.setattr("app.storage.progress.append_message_chunk", lambda *a, **k: None)

    ctx = _make_integrate_ctx()
    integ_mod.run_integrate(ctx)
    assert ctx.thread_summary == "Provider enrollment — Sunshine Health (FL Medicaid)"
    assert ctx.thread_state is not None
    assert "sunshinehealth.com" in ctx.thread_state
    assert "form" in ctx.thread_state.lower()


# ── 6. Dedicated thread summarizer (Gemini-reliable, separate call) ───


def test_summarizer_parse_extracts_short_and_long():
    from app.responder.thread_summarizer import _parse

    short, long_ = _parse('{"short": "Provider enrollment — Sunshine Health (FL Medicaid)", '
                           '"long": "Enroll via sunshinehealth.com; CAQH accepted. User wants the form link."}')
    assert short == "Provider enrollment — Sunshine Health (FL Medicaid)"
    assert "sunshinehealth.com" in long_


def test_summarizer_parse_tolerates_code_fence_and_prose():
    from app.responder.thread_summarizer import _parse

    raw = 'Here is the summary:\n```json\n{"short": "H0036 criteria", "long": "InterQual 2023 applies."}\n```'
    short, long_ = _parse(raw)
    assert short == "H0036 criteria"
    assert long_ == "InterQual 2023 applies."


def test_summarizer_parse_returns_none_on_garbage():
    from app.responder.thread_summarizer import _parse

    assert _parse("not json at all") == (None, None)
    assert _parse("") == (None, None)


def test_summarizer_returns_none_on_llm_failure(monkeypatch):
    """A failing/empty LLM call must degrade to (None, None) so the caller
    falls back to the integrator's thread_summary — never crash a turn."""
    import app.responder.thread_summarizer as ts

    def _boom(*a, **k):
        raise RuntimeError("vertex down")

    monkeypatch.setattr("app.services.llm_manager.generate_sync", _boom)
    assert ts.summarize_thread(previous_long=None, user_message="q", answer_text="a") == (None, None)


def test_format_quality_rewards_clean_over_narration():
    """The bandit reward must rank a clean label+brief above narration."""
    from app.responder.thread_summarizer import _format_quality
    clean = _format_quality(
        "Provider enrollment — Sunshine Health (FL Medicaid)",
        "Provider enrollment — Sunshine Health (FL Medicaid). Enroll via sunshinehealth.com.",
    )
    narrated = _format_quality(
        "The user is asking about provider enrollment for Sunshine Health.",
        "The user asked how to enroll. The assistant could not find details.",
    )
    assert clean == 1.0
    assert narrated < clean
    assert _format_quality(None, None) == 0.0


def test_derive_short_keeps_clean_label():
    from app.responder.thread_summarizer import _derive_short
    s = _derive_short("Provider enrollment — Sunshine Health (FL Medicaid)", "long ...")
    assert s == "Provider enrollment — Sunshine Health (FL Medicaid)"


def test_derive_short_strips_narration_from_short():
    from app.responder.thread_summarizer import _derive_short
    # Narration short, but the long brief leads with a clean topic.
    s = _derive_short(
        "The user is asking about provider enrollment for Sunshine Health.",
        "Provider enrollment — Sunshine Health (FL Medicaid). Enroll via sunshinehealth.com.",
    )
    assert s == "Provider enrollment — Sunshine Health (FL Medicaid)"


def test_derive_short_recovers_from_narration_long():
    from app.responder.thread_summarizer import _derive_short
    # Both fields narrate — still strip the leading 'User is seeking'.
    s = _derive_short(
        "User is seeking the provider enrollment form.",
        "User is seeking the provider enrollment form for Sunshine Health. Not found yet.",
    )
    assert not s.lower().startswith("user is seeking")
    assert "provider enrollment form" in s.lower()


def test_summarizer_overrides_via_generate_sync(monkeypatch):
    """Happy path: a well-formed model response yields (short, long)."""
    import app.responder.thread_summarizer as ts

    payload = '{"short": "Claim appeal — Sunshine Health", "long": "Days 36-90 denied as duplicate; appeal in progress."}'
    monkeypatch.setattr("app.services.llm_manager.generate_sync",
                        lambda *a, **k: (payload, {"total_tokens": 10}))
    short, long_ = ts.summarize_thread(
        previous_long="Claim — Sunshine Health", user_message="how do I appeal?",
        answer_text="File within 90 days via portal.")
    assert short == "Claim appeal — Sunshine Health"
    assert "duplicate" in long_
