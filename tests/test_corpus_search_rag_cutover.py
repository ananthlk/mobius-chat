"""Phase 1 RAG endpoint cutover (2026-08-06) — corpus_search.py swapped off
the legacy corpus_search_agent skill onto RAG's production
/api/retriever/answer endpoint. Chat Architecture's full spec, coordinated
with Retriever/RAG over several rounds — see project memory for the
mode_override naming collision, the field-consumption inventory, and the
fact-store "s"-chunk finding this cutover is built against.

These tests lock in:
  - the new URL + request body shape (query/caller_mode/
    token_budget_for_retrieval/citable_required only — no
    mode/k/filters/assembly_strategy/canonical_floor, no
    allocator_override/authority_requirement)
  - per-chunk field remapping per Chat Architecture's table (direct,
    renamed, and derived fields)
  - confidence_label derivation from rerank_score
  - retrieval_arms derivation from filler_strategy
  - the "s"-chunk golden-flag early-return is GONE (chunks flow through
    the same generic mapping, no special treatment yet — deliberately
    not built solo, pending a Retriever pairing session)
  - zero-chunks / non-ok status handling
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.skills.builtin.corpus_search import _derive_confidence_label, _run
from app.skills.registry import SkillCall

_BASE_CHUNK = {
    "index": 1,
    "chunk_id": "c1",
    "text": "Prior auth is required for H0036 under FL Medicaid.",
    "document_name": "provider_manual.pdf",
    "source_type": "internal",
    "document_id": "doc-1",
    "url": None,
    "page_number": 12,
    "paragraph_index": 3,
    "document_status": "completed",
    "authority": "high",
    "verified": True,
    "is_neighbor": False,
    "original_score": 0.81,
    "rerank_score": 0.62,
    "filler_strategy": "b",
    "slot_id": "s1",
    "slot_semantics": "single_fact",
}


def _make_call(query: str, inputs_extra: dict | None = None) -> SkillCall:
    return SkillCall(
        name="search_corpus",
        inputs={"query": query, **(inputs_extra or {})},
        question=query,
        user_message=query,
        thread_id="t1",
        active_context={},
        mode="copilot",
        emitter=lambda m: None,
        pipeline_ctx=MagicMock(correlation_id="test-cid"),
        extra_out=None,
    )


def _mock_urlopen_returning(response_body: dict, captured: dict | None = None):
    def _urlopen(req, timeout=None):
        if captured is not None:
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["headers"] = dict(req.headers)
        resp = MagicMock()
        resp.read.return_value = json.dumps(response_body).encode()
        resp.__enter__ = lambda self: resp
        resp.__exit__ = lambda self, *a: None
        return resp
    return _urlopen


def _run_with_response(query: str, contract_extra: dict, inputs_extra: dict | None = None, captured: dict | None = None):
    response = {
        "contract": {
            "query": query, "chosen_slot": None, "score": None,
            "chunks": [], "answer_text": None, "thinking": None,
            "traces": {}, "routing_keys": {}, "grounding_markers": {},
            "latency_ms": {}, "attempt_count": 1, "status": "ok",
            **contract_extra,
        },
        "latency_ms": {"total_ms": 500},
        "dispatch_path": "greedy",
        "allocator_override": None,
        "authority_requirement": None,
        "strategies_per_slot": [],
    }
    call = _make_call(query, inputs_extra)
    with patch.dict("os.environ", {"RAG_API_URL": "https://mobius-rag-ortabkknqa-uc.a.run.app"}), \
         patch("urllib.request.urlopen", side_effect=_mock_urlopen_returning(response, captured)), \
         patch("app.skills.builtin.corpus_search._persist_retrieval_run"), \
         patch("app.skills.builtin.corpus_search._emit_retrieval_trace_envelope"):
        return _run(call)


# ── Request shape ────────────────────────────────────────────────────


def test_dispatches_to_the_new_production_endpoint():
    captured: dict = {}
    _run_with_response("q", {}, captured=captured)
    assert captured["url"] == "https://mobius-rag-ortabkknqa-uc.a.run.app/api/retriever/answer"


def test_request_body_has_only_the_new_contract_fields():
    captured: dict = {}
    _run_with_response("Is prior authorization required for H0036?", {}, inputs_extra={"citable_required": True}, captured=captured)
    assert captured["body"] == {
        "query": "Is prior authorization required for H0036?",
        "citable_required": True,
        # correlation_id: 2026-08-06 spec amendment, see the dedicated
        # tests below for why this is here.
        "correlation_id": "test-cid",
        # caller_mode: always sent now (call.mode -- see below), not
        # conditional on anything else in the request.
        "caller_mode": "copilot",
    }


def test_caller_mode_defaults_to_call_mode():
    """2026-08-06, Chat Architecture clarification: caller_mode is chat's
    own quick/copilot/agentic/task chat_mode, sent via call.mode (which
    every react_loop.py dispatch site already sets to ctx.chat_mode) --
    not the LLMManager v2 speed-tier vocabulary an earlier revision of
    this code guessed at. Always sent, not conditional."""
    captured: dict = {}
    _run_with_response("q", {}, captured=captured)  # _make_call sets mode="copilot"
    assert captured["body"].get("caller_mode") == "copilot"


def test_caller_mode_explicit_input_override_wins_over_call_mode():
    captured: dict = {}
    _run_with_response("q", {}, inputs_extra={"caller_mode": "agentic"}, captured=captured)
    assert captured["body"].get("caller_mode") == "agentic"


def test_correlation_id_sent_unconditionally_for_the_grading_callback_fix():
    """2026-08-06 spec amendment: RAG's grade endpoint filters WHERE
    correlation_id = :cid on the DB -- chat's own turn correlation_id
    (same value already sent as X-Caller-Id) must ALSO be in the body,
    unconditionally, not gated on citable_required or any other flag.
    Inert until Retriever's persist_decision() fix deploys, but must be
    sent now regardless."""
    captured: dict = {}
    _run_with_response("q", {}, captured=captured)  # no citable_required, no special inputs
    assert captured["body"].get("correlation_id") == "test-cid"


def test_correlation_id_matches_the_x_caller_id_header():
    """Same underlying value, two transports (header + new body field) --
    must never drift apart."""
    captured: dict = {}
    _run_with_response("q", {}, captured=captured)
    assert captured["body"].get("correlation_id") == captured["headers"].get("X-caller-id")


def test_request_body_never_carries_legacy_or_router_internal_params():
    captured: dict = {}
    _run_with_response("q", {}, inputs_extra={"citable_required": True}, captured=captured)
    for legacy_key in ("mode", "k", "filters", "assembly_strategy", "canonical_floor", "include_document_ids"):
        assert legacy_key not in captured["body"], f"{legacy_key!r} should not appear in the new request body"
    for router_internal_key in ("allocator_override", "mode_override", "authority_requirement"):
        assert router_internal_key not in captured["body"], f"{router_internal_key!r} must stay off chat's request body"


def test_token_budget_passes_through_when_present():
    """token_budget_for_retrieval is passthrough-only, no inference --
    react_loop.py doesn't currently supply it, but whatever IS present
    on inputs must reach the body unmodified."""
    captured: dict = {}
    _run_with_response("q", {}, inputs_extra={"token_budget_for_retrieval": 4000}, captured=captured)
    assert captured["body"].get("token_budget_for_retrieval") == 4000


# ── Per-chunk field remapping ────────────────────────────────────────


def test_direct_fields_map_unchanged():
    env = _run_with_response("q", {"chunks": [_BASE_CHUNK]})
    s = env.sources[0]
    assert s.text == "Prior auth is required for H0036 under FL Medicaid."
    assert s.document_name == "provider_manual.pdf"
    assert s.document_id == "doc-1"
    assert s.page_number == 12
    assert s.extra["paragraph_index"] == 3


def test_authority_level_renamed_to_authority():
    env = _run_with_response("q", {"chunks": [_BASE_CHUNK]})
    assert env.sources[0].authority == "high"


def test_rerank_score_and_original_score_kept_distinct():
    """original_score is NOT aliased to the old `similarity` field --
    different formula per Chat Architecture, kept under its own name."""
    env = _run_with_response("q", {"chunks": [_BASE_CHUNK]})
    s = env.sources[0]
    assert s.extra["rerank_score"] == 0.62
    assert s.extra["original_score"] == 0.81
    assert "similarity" not in s.extra


def test_confidence_label_derived_from_rerank_score():
    env = _run_with_response("q", {"chunks": [_BASE_CHUNK]})  # rerank_score=0.62
    assert env.sources[0].extra["confidence_label"] == "high"


def test_retrieval_arms_derived_from_filler_strategy():
    env = _run_with_response("q", {"chunks": [_BASE_CHUNK]})  # filler_strategy="b"
    assert env.sources[0].extra["retrieval_arms"] == ["b"]
    assert env.sources[0].extra["filler_strategy"] == "b"


def test_retrieval_arms_empty_when_no_filler_strategy():
    chunk = {**_BASE_CHUNK, "filler_strategy": None}
    env = _run_with_response("q", {"chunks": [chunk]})
    assert env.sources[0].extra["retrieval_arms"] == []


def test_payer_and_state_are_none_phase_1():
    """Not threaded through by RAG yet (Phase 2) -- must be present as
    None, not silently absent, so downstream code checking `.get("payer")`
    behaves identically to before rather than KeyError-ing."""
    env = _run_with_response("q", {"chunks": [_BASE_CHUNK]})
    s = env.sources[0]
    assert s.extra["payer"] is None
    assert s.extra["state"] is None


def test_tags_carries_the_raw_dict():
    chunk = {**_BASE_CHUNK, "tags": {"payor": "sunshine_health"}}
    env = _run_with_response("q", {"chunks": [chunk]})
    assert env.sources[0].extra["tags"] == {"payor": "sunshine_health"}


# ── confidence_label thresholds (pure function) ─────────────────────


class TestDeriveConfidenceLabel:
    def test_none_is_abstain(self):
        assert _derive_confidence_label(None) == "abstain"

    def test_high(self):
        assert _derive_confidence_label(0.55) == "high"
        assert _derive_confidence_label(0.9) == "high"

    def test_medium(self):
        assert _derive_confidence_label(0.35) == "medium"
        assert _derive_confidence_label(0.54) == "medium"

    def test_low(self):
        assert _derive_confidence_label(0.18) == "low"
        assert _derive_confidence_label(0.34) == "low"

    def test_below_low_is_abstain(self):
        assert _derive_confidence_label(0.0) == "abstain"
        assert _derive_confidence_label(0.17) == "abstain"


# ── "s"-strategy chunks: no special treatment (deliberately, this pass) ──


def test_s_strategy_chunks_flow_through_generic_mapping_no_early_return():
    """The old golden-flag early-return pattern is REMOVED per spec. An
    "s"-tagged (fact-store) chunk gets the same SourceRef construction
    as any other chunk -- no early return, no extra["golden"] flag
    anywhere -- confirmed correct by Retriever (2026-08-06): escalation
    decisions belong to the Router, reflected in the response's own
    status/chosen_slot, not something this file should re-derive from
    seeing an "s" chunk."""
    s_chunk = {**_BASE_CHUNK, "filler_strategy": "s"}
    env = _run_with_response("q", {"chunks": [s_chunk]})
    assert env.extra.get("golden") is None  # no golden flag anywhere
    assert env.sources[0].extra["filler_strategy"] == "s"
    assert env.sources[0].extra["retrieval_arms"] == ["s"]


def test_s_strategy_chunk_confidence_label_is_always_high():
    """2026-08-06, Retriever confirmed via code read: filler_s.py never
    sets rerank_score on "s" chunks (always None) -- these are certified
    facts, not a probabilistic retrieval match, so rerank_score's "how
    well did this match" question doesn't apply. Running None through
    _derive_confidence_label would give "abstain", the worst label, for
    exactly the chunks that deserve the best one. Real-world shape: no
    rerank_score at all on these chunks."""
    s_chunk = {**_BASE_CHUNK, "filler_strategy": "s", "rerank_score": None}
    env = _run_with_response("q", {"chunks": [s_chunk]})
    assert env.sources[0].extra["confidence_label"] == "high"


def test_fact_store_chunk_confidence_label_is_always_high():
    """The REAL value observed in production (2026-08-06, live smoke-test
    against the deployed endpoint): filler_strategy=="fact_store", not
    "s" as Retriever described verbally. A literal `== "s"` check never
    matched anything live -- this test locks in the actual value that
    matters; the "s" test above is kept in case both appear somewhere."""
    fact_store_chunk = {**_BASE_CHUNK, "filler_strategy": "fact_store", "rerank_score": None}
    env = _run_with_response("q", {"chunks": [fact_store_chunk]})
    assert env.sources[0].extra["confidence_label"] == "high"


def test_non_s_chunk_with_null_rerank_score_still_abstains():
    """Control: the "always high" special-case is keyed on
    filler_strategy=="s" specifically, not on rerank_score being None in
    general -- a normal chunk that happens to have no score still
    correctly abstains rather than getting the "s"-only treatment."""
    chunk = {**_BASE_CHUNK, "filler_strategy": "b", "rerank_score": None}
    env = _run_with_response("q", {"chunks": [chunk]})
    assert env.sources[0].extra["confidence_label"] == "abstain"


def test_s_strategy_authority_and_chunk_grain_already_correct_no_change_needed():
    """2026-08-06, Retriever confirmed: authority_level="contract_source_
    of_truth" is hardcoded server-side and RAG's own synthesis.py already
    resolves it to authority: "authoritative" before the response reaches
    chat -- and source_type=="fact_store" (also hardcoded server-side) is
    the distinct marker, already captured under extra["chunk_grain"] by
    the existing generic mapping. Locks in that this file needs (and
    gets) no special-case for either field."""
    s_chunk = {
        **_BASE_CHUNK, "filler_strategy": "s", "rerank_score": None,
        "authority": "authoritative", "source_type": "fact_store",
    }
    env = _run_with_response("q", {"chunks": [s_chunk]})
    s = env.sources[0]
    assert s.authority == "authoritative"
    assert s.extra["chunk_grain"] == "fact_store"


def test_no_early_return_even_when_status_and_chosen_slot_suggest_fact_store():
    """chosen_slot=="direct_answer" is explicitly NOT the "s"-strategy
    signal (Retriever's correction) -- confirms the code doesn't
    accidentally treat it as one."""
    s_chunk = {**_BASE_CHUNK, "filler_strategy": "s"}
    env = _run_with_response("q", {"chunks": [s_chunk], "chosen_slot": "direct_answer"})
    assert len(env.sources) == 1  # normal chunk path taken, not an early-return with empty sources


# ── Zero-chunks / status handling ─────────────────────────────────────
#
# 2026-08-06: gate is on chunks alone, NOT status. Real bug found + fixed
# during live smoke verification for this deploy -- the first version
# gated on `status not in (None, "ok")`, which silently discarded
# status=="partial" responses even when they carried real, useful chunks
# (confirmed via a direct call to the live endpoint: status="partial",
# 10 real chunks including a fact_store hit with chosen_slot=
# "direct_answer", score=1.0 -- "partial" means "not every slot filled,"
# not "nothing usable").


def test_empty_chunks_returns_no_sources():
    env = _run_with_response("q", {"chunks": [], "status": "ok"})
    assert env.signal == "no_sources"
    assert env.sources == []


def test_partial_status_with_real_chunks_still_succeeds():
    """The actual regression this locks in: partial status + real chunks
    is a normal, common, successful result -- must not be discarded."""
    env = _run_with_response("q", {"chunks": [_BASE_CHUNK], "status": "partial"})
    assert env.signal == "corpus_only"
    assert len(env.sources) == 1


def test_timeout_status_with_chunks_also_succeeds():
    """status alone never gates -- only chunks presence does, for any
    status value the new pipeline might report."""
    env = _run_with_response("q", {"chunks": [_BASE_CHUNK], "status": "timeout"})
    assert env.signal == "corpus_only"
    assert len(env.sources) == 1


def test_no_retrieval_status_clean_empty_result():
    env = _run_with_response("q", {"chunks": [], "status": "no_retrieval"})
    assert env.signal == "no_sources"
    assert env.text == ""


# ── Post-synthesis grading callback registration (2026-08-06) ───────────
#
# Re-keyed on correlation_id -- see the module docstring in
# corpus_search.py and tests/test_orchestrator.py's
# test_fire_rag_grade_callbacks_* for the PATCH-firing half of this.


def test_grading_callback_registered_on_successful_search():
    call = _make_call("q")
    call.pipeline_ctx.pending_rag_grade_calls = []  # real list, not a MagicMock auto-attr
    response = {
        "contract": {"chunks": [_BASE_CHUNK], "status": "ok"},
        "latency_ms": {}, "dispatch_path": "greedy",
        "allocator_override": None, "authority_requirement": None,
    }
    with patch.dict("os.environ", {"RAG_API_URL": "https://mobius-rag-ortabkknqa-uc.a.run.app"}), \
         patch("urllib.request.urlopen", side_effect=_mock_urlopen_returning(response)), \
         patch("app.skills.builtin.corpus_search._persist_retrieval_run"), \
         patch("app.skills.builtin.corpus_search._emit_retrieval_trace_envelope"):
        _run(call)

    pending = call.pipeline_ctx.pending_rag_grade_calls
    assert len(pending) == 1
    assert pending[0]["base_url"] == "https://mobius-rag-ortabkknqa-uc.a.run.app"
    assert pending[0]["correlation_id"] == "test-cid"
    assert pending[0]["query"] == "q"
    assert pending[0]["chunks"] == [_BASE_CHUNK]


def test_grading_callback_not_registered_when_no_chunks():
    call = _make_call("q")
    call.pipeline_ctx.pending_rag_grade_calls = []
    response = {
        "contract": {"chunks": [], "status": "no_retrieval"},
        "latency_ms": {}, "dispatch_path": "greedy",
        "allocator_override": None, "authority_requirement": None,
    }
    with patch.dict("os.environ", {"RAG_API_URL": "https://mobius-rag-ortabkknqa-uc.a.run.app"}), \
         patch("urllib.request.urlopen", side_effect=_mock_urlopen_returning(response)), \
         patch("app.skills.builtin.corpus_search._persist_retrieval_run"), \
         patch("app.skills.builtin.corpus_search._emit_retrieval_trace_envelope"):
        _run(call)

    assert call.pipeline_ctx.pending_rag_grade_calls == []
