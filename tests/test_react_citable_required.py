"""citable_required passthrough to RAG's corpus_search_agent contract.

2026-08-05, Chat Architecture ruling (coordinated with Retriever/RAG):
RAG's corpus_search_agent gained a real citable_required param, previously
dead (no caller could set it). Chat decides this one; authority_requirement
and allocator_override (renamed from mode_override to resolve a collision
with react's own internal mode cascade state) stay off chat's request body
entirely -- those are Router-internal gates.

Deterministic keyword rule, not new inference: queries that inherently
demand verifiable sources (payor policy, prior auth, coverage, claims) get
citable_required=True; everything else omits the key so the Router's own
default applies. Checked concretely that no existing planner signal
already classifies this before choosing the keyword-rule approach.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import _citable_required, _execute_tool

_SEARCH_SUCCESS = {
    "tool": "search_corpus", "success": True,
    "result": "a real, sufficiently long, useful snippet about the policy in question",
    "signal": "corpus_only", "sources": [], "usage": None,
}


class TestCitableRequiredKeywordRule:
    def test_default_is_false_for_unrelated_query(self):
        assert _citable_required("What is the ICD-10 code for major depressive disorder?") is False

    def test_empty_or_none_is_false(self):
        assert _citable_required("") is False
        assert _citable_required(None) is False

    def test_prior_auth_triggers_true(self):
        assert _citable_required("Does H0036 need prior authorization?") is True

    def test_coverage_triggers_true(self):
        assert _citable_required("What is the coverage policy for telehealth intake?") is True

    def test_covered_triggers_true(self):
        """2026-08-05 (Chat Architecture, approved): "covered" and "covers"
        added to close a real gap -- "coverage" alone missed "is X covered"
        phrasing, which is common in practice (Ananth's own live queries
        this session used exactly this wording multiple times)."""
        assert _citable_required("Is telehealth covered for behavioral health intake?") is True

    def test_covers_triggers_true(self):
        assert _citable_required("Which services does this plan covers?") is True

    def test_bare_cover_without_stem_is_a_known_narrower_residual_gap(self):
        """"cover" (base form, no -s/-ed/-age suffix) still doesn't match --
        e.g. "does this plan cover X" with singular "does". Narrower than
        the original gap (covered/covers now both close), not asserting
        this is correct, just locking in current behavior."""
        assert _citable_required("Does this plan cover speech therapy?") is False

    def test_payor_triggers_true(self):
        assert _citable_required("What is Sunshine Health's payor slug?") is True

    def test_claim_and_denial_trigger_true(self):
        assert _citable_required("Why was this claim denied?") is True

    def test_medical_necessity_triggers_true(self):
        assert _citable_required("What documentation shows medical necessity for this service?") is True

    def test_case_insensitive(self):
        assert _citable_required("PRIOR AUTHORIZATION requirements") is True

    def test_formulary_triggers_true(self):
        assert _citable_required("Is this drug on the formulary?") is True


def _make_ctx(message: str, chat_mode: str = "copilot") -> PipelineContext:
    ctx = PipelineContext(correlation_id="citable-test", thread_id=None, message=message)
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = chat_mode
    ctx.thinking_chunks = []
    return ctx


def test_citable_required_true_reaches_the_skill_call_inputs():
    """_execute_tool() (not the retry wrapper -- that unconditionally
    imports app.communication.error_emit, which needs a package this test
    env doesn't have, see test_tool_manifest.py's collection-error note
    elsewhere in this suite) must set citable_required=True on the actual
    SkillCall dispatched to search_corpus for a coverage-shaped query."""
    captured_calls = []

    def fake_dispatch(call):
        captured_calls.append(call)
        env = MagicMock()
        env.text = "some retrieved policy text"
        env.sources = []
        env.signal = "corpus_only"
        env.extra = {}
        return env

    ctx = _make_ctx("Is prior authorization required for H0036?")
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        _execute_tool("rag", {"query": "Is prior authorization required for H0036?"}, ctx, emitter=None)

    search_calls = [c for c in captured_calls if c.name == "search_corpus"]
    assert search_calls, "expected at least one search_corpus dispatch"
    assert search_calls[0].inputs.get("citable_required") is True


def test_citable_required_omitted_for_unrelated_query():
    """Control case: a query with no citable-source signal must NOT carry
    the key at all (omitted, not False) -- the Router's own default applies."""
    captured_calls = []

    def fake_dispatch(call):
        captured_calls.append(call)
        env = MagicMock()
        env.text = "some retrieved clinical text"
        env.sources = []
        env.signal = "corpus_only"
        env.extra = {}
        return env

    ctx = _make_ctx("What is the ICD-10 code for major depressive disorder?")
    with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
        _execute_tool("rag", {"query": "What is the ICD-10 code for major depressive disorder?"}, ctx, emitter=None)

    search_calls = [c for c in captured_calls if c.name == "search_corpus"]
    assert search_calls, "expected at least one search_corpus dispatch"
    assert "citable_required" not in search_calls[0].inputs


class TestRagRelaxThenReframeProtocol:
    """2026-08-06, Chat Architecture spec: the 3-call bounded protocol that
    replaced the dead mode="auto"->"d" arms cascade. Call 1 uses the
    keyword-rule baseline; if it's citable_required=True and comes back
    empty, call 2 automatically relaxes citable_required to learn from
    non-citable sources; call 3 restores citable_required expecting a
    reformulated query. Call 4+ is a hard, code-level stop -- "non-
    negotiable, no fallback to more grinding rounds" -- verified here by
    asserting the network is never actually hit a 4th time, not just that
    the prompt asks the LLM nicely not to."""

    @staticmethod
    def _empty_dispatch(status: str = "no_retrieval"):
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
                }
            }
            return env

        return calls, fake_dispatch

    def test_call_1_uses_baseline_citable_required(self):
        calls, fake_dispatch = self._empty_dispatch()
        ctx = _make_ctx("Is prior authorization required for H0036?")
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
            _execute_tool("rag", {"query": "Is prior authorization required for H0036?"}, ctx, emitter=None)
        assert calls[0].inputs.get("citable_required") is True

    def test_call_2_relaxes_citable_required_after_empty_citable_call_1(self):
        calls, fake_dispatch = self._empty_dispatch()
        ctx = _make_ctx("Is prior authorization required for H0036?")
        query = "Is prior authorization required for H0036?"
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
            _execute_tool("rag", {"query": query}, ctx, emitter=None)  # call 1
            _execute_tool("rag", {"query": query}, ctx, emitter=None)  # call 2
        assert calls[0].inputs.get("citable_required") is True
        assert "citable_required" not in calls[1].inputs

    def test_call_3_reframes_with_citable_required_restored(self):
        calls, fake_dispatch = self._empty_dispatch()
        ctx = _make_ctx("Is prior authorization required for H0036?")
        query = "Is prior authorization required for H0036?"
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
            _execute_tool("rag", {"query": query}, ctx, emitter=None)  # call 1
            _execute_tool("rag", {"query": query}, ctx, emitter=None)  # call 2 (relaxed)
            _execute_tool("rag", {"query": query}, ctx, emitter=None)  # call 3 (reframed)
        assert len(calls) == 3
        assert calls[2].inputs.get("citable_required") is True

    def test_call_4_is_a_hard_stop_no_network_dispatch(self):
        """The non-negotiable part: call 4 must not reach dispatch() at
        all -- confirmed by asserting call count stays at 3, not by
        trusting the prompt instruction."""
        calls, fake_dispatch = self._empty_dispatch()
        ctx = _make_ctx("Is prior authorization required for H0036?")
        query = "Is prior authorization required for H0036?"
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
            for _ in range(3):
                _execute_tool("rag", {"query": query}, ctx, emitter=None)
            result4 = _execute_tool("rag", {"query": query}, ctx, emitter=None)

        assert len(calls) == 3, "call 4 must not hit the network"
        assert result4["success"] is False
        assert "RAG_BUDGET_EXHAUSTED" in result4["result"]
        assert result4.get("rag_call_number") == 4

    def test_agentic_mode_raises_ceiling_to_6_call_7_is_the_hard_stop(self):
        """2026-08-16, Ananth's direct call (via Retriever): agentic
        (chat.thinking) has real headroom the 2026-08-06 ceiling didn't
        account for -- raised to 6 for this mode only. Every other mode
        (default chat_mode="copilot" here) keeps the original ceiling of 3,
        covered by test_call_4_is_a_hard_stop_no_network_dispatch above."""
        calls, fake_dispatch = self._empty_dispatch()
        ctx = _make_ctx("Is prior authorization required for H0036?", chat_mode="agentic")
        query = "Is prior authorization required for H0036?"
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
            for _ in range(6):
                _execute_tool("rag", {"query": query}, ctx, emitter=None)
            result7 = _execute_tool("rag", {"query": query}, ctx, emitter=None)

        assert len(calls) == 6, "agentic mode should allow all 6 calls through to dispatch"
        assert result7["success"] is False
        assert "RAG_BUDGET_EXHAUSTED" in result7["result"]
        assert result7.get("rag_call_number") == 7

    def test_relax_never_triggers_when_call_1_already_had_chunks(self):
        """Control: a call 1 that returns real chunks must not relax on
        call 2 -- relax is specifically for empty citable-required results,
        not a default behavior on every second call."""
        call_log = []

        def fake_dispatch(call):
            call_log.append(call)
            env = MagicMock()
            if len(call_log) == 1:
                env.text = "a real, sufficiently long, useful snippet about the policy"
                env.sources = []
                env.signal = "corpus_only"
                env.extra = {"pipeline_trace": {"status": "ok", "n_chunks": 3, "dispatch_path": "a", "chosen_slot": "direct_answer"}}
            else:
                env.text = ""
                env.sources = []
                env.signal = "no_sources"
                env.extra = {"pipeline_trace": {"status": "no_retrieval", "n_chunks": 0, "dispatch_path": "a", "chosen_slot": None}}
            return env

        ctx = _make_ctx("Is prior authorization required for H0036?")
        query = "Is prior authorization required for H0036?"
        with patch("app.skills.registry.dispatch", side_effect=fake_dispatch):
            _execute_tool("rag", {"query": query}, ctx, emitter=None)  # call 1: real chunks
            _execute_tool("rag", {"query": query}, ctx, emitter=None)  # call 2: should just repeat baseline

        assert call_log[1].inputs.get("citable_required") is True, (
            "call 2 must not relax when call 1 already returned usable chunks"
        )
