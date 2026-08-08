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
from app.pipeline.react_loop import (
    _execute_tool,
    _is_specific_clarify_question,
    _clarify_context_already_resolves,
    _should_bypass_on_clarify,
)


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


# ── False-positive guard (2026-08-08, Chat Master follow-up) ───────────────
# The RAG classifier occasionally fires CLARIFY on a specific, well-formed
# query. _should_bypass_on_clarify gates the terminal stop on two checks:
# (1) the clarify text must name the actual ambiguity, not generic
# boilerplate; (2) chat's own carried-forward jurisdiction must not already
# resolve it. Either check failing means the signal is advisory.

class TestIsSpecificClarifyQuestion:
    def test_generic_boilerplate_rejected(self):
        assert _is_specific_clarify_question("Could you clarify what topic this relates to?") is False

    def test_generic_more_details_rejected(self):
        assert _is_specific_clarify_question("Can you provide more details?") is False

    def test_names_a_domain_term_accepted(self):
        assert _is_specific_clarify_question("Which payer — Sunshine Health or Simply Healthcare?") is True

    def test_names_a_code_accepted(self):
        assert _is_specific_clarify_question("Are you asking about CPT 99213 or 99214?") is True

    def test_proper_noun_mid_sentence_accepted(self):
        assert _is_specific_clarify_question("Do you mean Sunshine Health's policy or Simply Healthcare's?") is True

    def test_empty_string_rejected(self):
        assert _is_specific_clarify_question("") is False
        assert _is_specific_clarify_question(None) is False


class TestClarifyContextAlreadyResolves:
    def test_state_in_query_and_active_resolves(self):
        active = {"jurisdiction": "Florida"}
        assert _clarify_context_already_resolves("What is the Florida timely filing deadline?", active) is True

    def test_payer_in_query_and_active_resolves(self):
        active = {"payer": "Sunshine Health"}
        assert _clarify_context_already_resolves("What is Sunshine Health's timely filing deadline?", active) is True

    def test_no_overlap_does_not_resolve(self):
        active = {"jurisdiction": "Florida", "payer": "Sunshine Health"}
        assert _clarify_context_already_resolves("What is the timely filing deadline?", active) is False

    def test_empty_active_does_not_resolve(self):
        assert _clarify_context_already_resolves("What is Sunshine Health's deadline?", {}) is False
        assert _clarify_context_already_resolves("What is Sunshine Health's deadline?", None) is False


class TestShouldBypassOnClarify:
    def test_specific_and_unresolved_bypasses(self):
        assert _should_bypass_on_clarify(
            ["Which payer — Sunshine Health or Simply Healthcare?"],
            "What's the timely filing deadline?",
            {},
        ) is True

    def test_generic_does_not_bypass(self):
        assert _should_bypass_on_clarify(
            ["Could you clarify what topic this relates to?"],
            "What's the Sunshine Health timely filing deadline?",
            {},
        ) is False

    def test_specific_but_context_resolves_does_not_bypass(self):
        assert _should_bypass_on_clarify(
            ["Which payer — Sunshine Health or Simply Healthcare?"],
            "What's Sunshine Health's timely filing deadline?",
            {"payer": "Sunshine Health"},
        ) is False

    def test_empty_list_does_not_bypass(self):
        assert _should_bypass_on_clarify([], "anything", {}) is False


def test_generic_clarify_does_not_bypass_and_retries_non_citable():
    """Integration: a generic/classifier-failure clarify question must NOT
    stop the turn -- react retries once with citable_required off and uses
    that result if it found anything."""
    calls = []

    def fake_dispatch(call):
        calls.append(call)
        env = MagicMock()
        if len(calls) == 1:
            env.text = ""
            env.sources = []
            env.signal = "no_sources"
            env.extra = {
                "pipeline_trace": {
                    "status": "no_retrieval", "n_chunks": 0,
                    "dispatch_path": "a", "chosen_slot": None,
                    "clarify_questions": ["Could you clarify what topic this relates to?"],
                }
            }
        else:
            env.text = "Timely filing is 180 days from date of service, per policy section 4.2."
            src = MagicMock()
            src.to_dict.return_value = {"document_name": "policy.pdf", "text": "..."}
            env.sources = [src]
            env.signal = "corpus_only"
            env.extra = {"pipeline_trace": {"status": "ok", "n_chunks": 1}}
        return env

    ctx = _make_ctx("What's the Sunshine Health timely filing deadline?")
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert getattr(ctx, "react_bypass_integrate", False) is False
    assert len(calls) == 2, "expected exactly one fallback retry"
    assert "citable_required" not in calls[1].inputs
    assert "180 days" in result["result"]


def test_context_resolved_clarify_does_not_bypass_and_retries_non_citable():
    """Integration: chat already knows the payer from carried-forward
    jurisdiction and the query names it -- clarify is advisory, retry
    instead of asking the user something chat already knows."""
    calls = []

    def fake_dispatch(call):
        calls.append(call)
        env = MagicMock()
        if len(calls) == 1:
            env.text = ""
            env.sources = []
            env.signal = "no_sources"
            env.extra = {
                "pipeline_trace": {
                    "status": "no_retrieval", "n_chunks": 0,
                    "dispatch_path": "a", "chosen_slot": None,
                    "clarify_questions": ["Which payer — Sunshine Health or Simply Healthcare?"],
                }
            }
        else:
            env.text = "Timely filing is 180 days."
            env.sources = []
            env.signal = "corpus_only"
            env.extra = {"pipeline_trace": {"status": "ok", "n_chunks": 0}}
        return env

    ctx = _make_ctx("What's Sunshine Health's timely filing deadline?")
    ctx.merged_state = {"active": {"payer": "Sunshine Health"}}
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert getattr(ctx, "react_bypass_integrate", False) is False
    assert len(calls) == 2


def test_fallback_retry_still_empty_falls_through_normally():
    """If the non-citable fallback also finds nothing, don't bypass and
    don't loop again -- just fall through to the normal empty-result
    handling (reframe signal / eventual honest miss)."""
    calls = []

    def fake_dispatch(call):
        calls.append(call)
        env = MagicMock()
        env.text = ""
        env.sources = []
        env.signal = "no_sources"
        env.extra = {
            "pipeline_trace": {
                "status": "no_retrieval", "n_chunks": 0,
                "dispatch_path": "a", "chosen_slot": None,
                "clarify_questions": ["Could you clarify what topic this relates to?"],
            }
        }
        return env

    ctx = _make_ctx("What's the Sunshine Health timely filing deadline?")
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        result = _execute_tool("rag", {"query": ctx.message}, ctx, emitter=None)

    assert getattr(ctx, "react_bypass_integrate", False) is False
    assert len(calls) == 2
    assert result["success"] is False
