"""RAG's Observer verdict/reason surfaced for the reframe decision
(2026-08-07, Task #41(a), Chat Master directive).

routing_keys.observer_final_reasons/observer_final_verdicts are RAG's
own Observer agent explaining WHY it stopped searching a slot (e.g.
"vector_filled_to_capacity" -- a structural/capacity limit, confirmed
live via a real captured response). More authoritative than
gap_status's dispatch_path/chosen_slot/status equality heuristic, which
only infers stagnation indirectly. Threaded through three layers:
corpus_search.py extracts it from the raw RAG response, react_loop.py
carries it in ctx._rag_call_history, prompts.py surfaces it in
[Evidence Ledger] with an instruction that overrides materiality-gate
reasoning when present.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.pipeline.context import PipelineContext
from app.pipeline.react.prompts import build_reasoning_context
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


def _mock_urlopen_returning(response_body: dict):
    def _urlopen(req, timeout=None):
        resp = MagicMock()
        resp.read.return_value = json.dumps(response_body).encode()
        resp.__enter__ = lambda self: resp
        resp.__exit__ = lambda self, *a: None
        return resp
    return _urlopen


def _run_with_response(query: str, contract_extra: dict, inputs_extra: dict | None = None):
    from app.skills.builtin.corpus_search import _run

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
         patch("urllib.request.urlopen", side_effect=_mock_urlopen_returning(response)), \
         patch("app.skills.builtin.corpus_search._persist_retrieval_run"), \
         patch("app.skills.builtin.corpus_search._emit_retrieval_trace_envelope"):
        return _run(call)


class TestCorpusSearchExtractsObserverFields:
    def test_observer_reason_and_verdict_extracted_for_chosen_slot(self):
        env = _run_with_response("q", {
            "chosen_slot": "direct_answer",
            "chunks": [_BASE_CHUNK],
            "routing_keys": {
                "observer_final_reasons": {"direct_answer": "vector_filled_to_capacity"},
                "observer_final_verdicts": {"direct_answer": "SATISFIED"},
            },
        })
        assert env.extra["pipeline_trace"]["observer_final_reason"] == "vector_filled_to_capacity"
        assert env.extra["pipeline_trace"]["observer_final_verdict"] == "SATISFIED"

    def test_absent_when_routing_keys_missing(self):
        """Older/degraded RAG responses without routing_keys must not crash."""
        env = _run_with_response("q", {"chosen_slot": "direct_answer", "chunks": [_BASE_CHUNK]})
        assert env.extra["pipeline_trace"]["observer_final_reason"] is None
        assert env.extra["pipeline_trace"]["observer_final_verdict"] is None

    def test_only_the_chosen_slots_reason_is_pulled_not_other_slots(self):
        env = _run_with_response("q", {
            "chosen_slot": "direct_answer",
            "chunks": [_BASE_CHUNK],
            "routing_keys": {
                "observer_final_reasons": {
                    "direct_answer": "vector_filled_to_capacity",
                    "context_section": "no_candidates_found",
                },
            },
        })
        assert env.extra["pipeline_trace"]["observer_final_reason"] == "vector_filled_to_capacity"

    def test_zero_chunk_early_return_still_carries_observer_fields(self):
        """The zero-chunks early-return path reuses the SAME telemetry
        dict -- must not be a separate, unpatched construction."""
        env = _run_with_response("q", {
            "chosen_slot": "direct_answer",
            "chunks": [],
            "routing_keys": {
                "observer_final_reasons": {"direct_answer": "no_candidates_found"},
                "observer_final_verdicts": {"direct_answer": "EXHAUSTED"},
            },
        })
        assert env.extra["pipeline_trace"]["observer_final_reason"] == "no_candidates_found"
        assert env.extra["pipeline_trace"]["observer_final_verdict"] == "EXHAUSTED"


def _make_ctx() -> PipelineContext:
    ctx = PipelineContext(correlation_id="observer-test", thread_id=None, message="does it matter")
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = "agentic"
    ctx.thinking_chunks = []
    return ctx


def _call(dispatch_path="bayesian", chosen_slot="direct_answer", status="partial", **overrides):
    d = {"dispatch_path": dispatch_path, "chosen_slot": chosen_slot, "status": status}
    d.update(overrides)
    return d


class TestObserverSignalInEvidenceLedger:
    def test_observer_reason_and_verdict_rendered(self):
        history = [_call(observer_final_reason="vector_filled_to_capacity", observer_final_verdict="SATISFIED")]
        out = build_reasoning_context(
            _make_ctx(), [], 2, max_iterations=10,
            gap_status="progressing", rag_call_history=history,
        )
        assert "observer_final_reason: 'vector_filled_to_capacity'" in out
        assert "observer_final_verdict: 'SATISFIED'" in out
        assert "more authoritative than gap_status" in out

    def test_uses_the_latest_calls_observer_signal_not_an_earlier_one(self):
        history = [
            _call(observer_final_reason="no_candidates_found", observer_final_verdict="EXHAUSTED"),
            _call(observer_final_reason="vector_filled_to_capacity", observer_final_verdict="SATISFIED"),
        ]
        out = build_reasoning_context(
            _make_ctx(), [], 3, max_iterations=10,
            gap_status="progressing", rag_call_history=history,
        )
        assert "vector_filled_to_capacity" in out
        assert "no_candidates_found" not in out

    def test_absent_when_history_entries_have_no_observer_fields(self):
        """Legacy history entries (no observer_final_reason key) must not
        render a spurious 'None' line."""
        history = [_call()]
        out = build_reasoning_context(
            _make_ctx(), [], 2, max_iterations=10,
            gap_status="progressing", rag_call_history=history,
        )
        assert "observer_final_reason" not in out

    def test_rule_1b_mentions_observer_final_reason_override(self):
        from app.pipeline.react.prompts import REACT_CRITICAL_RULES_TEXT
        assert "observer_final_reason" in REACT_CRITICAL_RULES_TEXT
        assert "filled_to_capacity" in REACT_CRITICAL_RULES_TEXT
