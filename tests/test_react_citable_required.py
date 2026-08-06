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
