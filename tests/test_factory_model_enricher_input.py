"""Tests for the factory-model enricher input contract (Task #58, 2026-08-07):
rag_chunks (unified chunk pool, replaces sources_summary/source_texts),
tool_outputs (capped raw non-rag tool results), and reasoning_ledger
(flattened react_trace_rounds[].enrichment) -- integrate.py's consumption-side
build/cap functions that feed the thinner "formatter" enricher prompt."""
from __future__ import annotations

from app.pipeline.context import PipelineContext  # noqa: F401 -- import order avoids a circular import
from app.stages.integrate import (
    _build_rag_chunks,
    _build_reasoning_ledger,
    _build_tool_outputs_for_prompt,
)


def _source(name="Doc A", text="chunk text", rerank=0.8, match=None, authority=None, conf=None, strategy=None):
    return {
        "document_name": name,
        "text": text,
        "rerank_score": rerank,
        "match_score": match,
        "authority": authority,
        "confidence_label": conf,
        "filler_strategy": strategy,
    }


class TestBuildRagChunks:
    def test_caps_at_seven_normal_mode(self):
        sources = [_source(name=f"Doc {i}", rerank=1.0 - i * 0.01) for i in range(15)]
        out = _build_rag_chunks(sources, None, "agentic")
        assert len(out) == 7

    def test_caps_at_four_quick_mode(self):
        sources = [_source(name=f"Doc {i}", rerank=1.0 - i * 0.01) for i in range(15)]
        out = _build_rag_chunks(sources, None, "quick")
        assert len(out) == 4

    def test_not_capped_to_seven_below_threshold(self):
        """The whole point of the redesign: the upstream pool can exceed 7 (react's
        keep-set), but a SMALL pool under the cap isn't artificially truncated."""
        sources = [_source(name=f"Doc {i}") for i in range(3)]
        out = _build_rag_chunks(sources, None, "agentic")
        assert len(out) == 3

    def test_sorted_by_rerank_score_descending(self):
        sources = [_source(name="Low", rerank=0.2), _source(name="High", rerank=0.9)]
        out = _build_rag_chunks(sources, None, "agentic")
        assert out[0]["document_name"] == "High"
        assert out[1]["document_name"] == "Low"

    def test_falls_back_to_match_score_when_no_rerank_score(self):
        sources = [_source(name="Low", rerank=None, match=0.1), _source(name="High", rerank=None, match=0.9)]
        out = _build_rag_chunks(sources, None, "agentic")
        assert out[0]["document_name"] == "High"

    def test_carries_authority_and_filler_strategy(self):
        sources = [_source(name="Doc A", authority="contract_source_of_truth", strategy="vector_rerank")]
        out = _build_rag_chunks(sources, None, "agentic")
        assert out[0]["authority"] == "contract_source_of_truth"
        assert out[0]["filler_strategy"] == "vector_rerank"

    def test_skips_sources_with_no_text(self):
        sources = [_source(name="Empty", text=""), _source(name="Real", text="content")]
        out = _build_rag_chunks(sources, None, "agentic")
        assert len(out) == 1
        assert out[0]["document_name"] == "Real"

    def test_folds_in_appeals_pseudo_sources(self):
        hints = [{"data": {"rules": [{"rule_id": "X.1", "rule_name": "Rule X.1", "rule_statement": "s", "appeal_argument": "a"}]}}]
        out = _build_rag_chunks([], hints, "agentic")
        assert len(out) == 1
        assert out[0]["document_name"] == "Rule X.1"
        assert out[0]["authority"] == "appeals_rule"

    def test_indices_are_contiguous_across_sources_and_appeals(self):
        sources = [_source(name="Doc A")]
        hints = [{"data": {"rules": [{"rule_id": "X.1", "rule_statement": "s", "appeal_argument": "a"}]}}]
        out = _build_rag_chunks(sources, hints, "agentic")
        assert [c["index"] for c in out] == [1, 2]

    def test_empty_inputs_return_empty(self):
        assert _build_rag_chunks([], None, "agentic") == []


class TestBuildToolOutputsForPrompt:
    def test_empty_or_none_returns_empty_dict(self):
        assert _build_tool_outputs_for_prompt(None) == {}
        assert _build_tool_outputs_for_prompt({}) == {}

    def test_passes_through_small_result_unmodified(self):
        out = _build_tool_outputs_for_prompt({"appeals_find_carc": [{"success": True, "result": "short", "result_summary": "s"}]})
        assert out["appeals_find_carc"] == [{"success": True, "result": "short", "result_summary": "s"}]

    def test_truncates_long_result_with_explicit_marker(self):
        long_result = "x" * 2000
        out = _build_tool_outputs_for_prompt({"appeals_lookup_rules": [{"success": True, "result": long_result}]})
        result = out["appeals_lookup_rules"][0]["result"]
        assert len(result) < 2000
        assert "truncated" in result

    def test_preserves_all_calls_when_under_ten(self):
        calls = [{"success": True, "result": f"call {i}"} for i in range(7)]
        out = _build_tool_outputs_for_prompt({"appeals_get_playbook": calls})
        assert len(out["appeals_get_playbook"]) == 7

    def test_caps_calls_per_tool_at_ten_with_note(self):
        calls = [{"success": True, "result": f"call {i}"} for i in range(15)]
        out = _build_tool_outputs_for_prompt({"appeals_get_playbook": calls})
        entries = out["appeals_get_playbook"]
        assert len(entries) == 11  # 10 calls + 1 omission note
        assert "note" in entries[-1]
        assert "5 more" in entries[-1]["note"]

    def test_multiple_tools_each_processed(self):
        out = _build_tool_outputs_for_prompt({
            "appeals_find_carc": [{"success": True, "result": "a"}],
            "lookup_authoritative_sources": [{"success": True, "result": "b"}],
        })
        assert set(out.keys()) == {"appeals_find_carc", "lookup_authoritative_sources"}

    def test_ignores_non_list_values(self):
        out = _build_tool_outputs_for_prompt({"weird_tool": "not a list"})
        assert out == {}


class TestBuildReasoningLedger:
    def test_empty_or_none_returns_empty(self):
        assert _build_reasoning_ledger(None) == []
        assert _build_reasoning_ledger([]) == []

    def test_rounds_without_enrichment_are_skipped(self):
        """Graceful degradation: enrichment field not yet landed on every round."""
        rounds = [{"round": 1, "tool": "rag", "directive": "explore"}]
        assert _build_reasoning_ledger(rounds) == []

    def test_extracts_enrichment_fields(self):
        """gaps is ONE free-text field as actually shipped (react_loop.py,
        2026-08-07) -- not split into gaps_closed/gaps_open lists (that was
        the originally proposed shape, flagged by ReAct as unbuilt)."""
        rounds = [{
            "round": 2, "tool": "appeals_find_carc",
            "enrichment": {"learned": "CARC 22 is a COB denial", "running_answer": "File a COB dispute", "gaps": "EOB on file?"},
        }]
        out = _build_reasoning_ledger(rounds)
        assert len(out) == 1
        entry = out[0]
        assert entry["round"] == 2
        assert entry["tool"] == "appeals_find_carc"
        assert entry["learned"] == "CARC 22 is a COB denial"
        assert entry["running_answer"] == "File a COB dispute"
        assert entry["gaps"] == "EOB on file?"

    def test_truncates_long_learned_running_answer_and_gaps(self):
        rounds = [{"round": 1, "enrichment": {"learned": "x" * 1000, "running_answer": "y" * 1000, "gaps": "z" * 1000}}]
        out = _build_reasoning_ledger(rounds)
        assert len(out[0]["learned"]) <= 500
        assert len(out[0]["running_answer"]) <= 500
        assert len(out[0]["gaps"]) <= 500

    def test_multiple_rounds_preserved_in_order(self):
        rounds = [
            {"round": 1, "enrichment": {"learned": "first"}},
            {"round": 2, "enrichment": {"learned": "second"}},
        ]
        out = _build_reasoning_ledger(rounds)
        assert [e["round"] for e in out] == [1, 2]

    def test_ignores_non_dict_rounds(self):
        assert _build_reasoning_ledger(["not a dict", None, 5]) == []
