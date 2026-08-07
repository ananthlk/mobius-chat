"""ctx.rag_chunks + ctx.tool_outputs (2026-08-07, Task #58, Chat
Architecture directive, Ananth's ruling on envelope taxonomy).

Two types only: (1) ctx.rag_chunks -- unified pool of every rag chunk
retrieved this turn, all filler arms merged, full per-chunk metadata
(authority/confidence/score/source/filler_strategy) -- an alias for the
already-full ctx.sources, exposed under a stable name so integrate.py
isn't limited to whatever top-N slice it happens to take. (2)
ctx.tool_outputs -- raw results from every non-rag tool call this turn,
keyed by tool name -> LIST (not a single value; a tool called more than
once in one turn, like appeals_get_playbook in the live COB trace, must
not lose every call but the last).

Same test pattern as test_section_hint_pipeline.py: SimpleNamespace ctx,
call _finalize_response directly, assert on the ctx fields it sets.
"""
from __future__ import annotations

import types

from app.pipeline.react_loop import _finalize_response


def _make_ctx(**kwargs):
    defaults = dict(
        chat_mode=None,
        system_context=None,
        usages=[],
        seed_tool_results=None,
        react_tool_results=None,
        effective_message="test query",
        message="test query",
        correlation_id="cid",
        thread_id="t1",
        extra_out=None,
        merged_state=None,
        user_profile=None,
    )
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _chunk(doc="Sunshine Provider Manual", authority="authoritative", rerank=0.6, strategy="vector_rerank"):
    return {
        "document_name": doc,
        "authority": authority,
        "rerank_score": rerank,
        "confidence_label": "high" if rerank and rerank >= 0.55 else "medium",
        "filler_strategy": strategy,
    }


class TestRagChunksUnifiedPool:
    def test_rag_chunks_mirrors_full_deduped_sources(self):
        chunks = [_chunk(doc="Doc A"), _chunk(doc="Doc B", strategy="fact_store")]
        ctx = _make_ctx()
        _finalize_response(ctx, "answer", chunks, "corpus_only", "rag", None)
        assert ctx.rag_chunks == ctx.sources
        assert len(ctx.rag_chunks) == 2

    def test_rag_chunks_not_capped_to_seven(self):
        """The whole point: integrate.py's source_texts caps at 7 for
        prompt-size reasons, but ctx.rag_chunks itself must carry
        everything -- capping is a downstream choice, not baked in here."""
        chunks = [_chunk(doc=f"Doc {i}") for i in range(12)]
        ctx = _make_ctx()
        _finalize_response(ctx, "answer", chunks, "corpus_only", "rag", None)
        assert len(ctx.rag_chunks) == 12

    def test_rag_chunks_carries_per_chunk_metadata(self):
        chunks = [_chunk(doc="Doc A", authority="contract_source_of_truth", rerank=0.9, strategy="vector_rerank")]
        ctx = _make_ctx()
        _finalize_response(ctx, "answer", chunks, "corpus_only", "rag", None)
        c = ctx.rag_chunks[0]
        assert c["authority"] == "contract_source_of_truth"
        assert c["filler_strategy"] == "vector_rerank"

    def test_no_arm_separation_all_fillers_in_one_list(self):
        """Different filler_strategy values (a/b/c/d/s equivalents) land
        in the SAME list -- arm is a per-chunk detail field, not a
        separate collection."""
        chunks = [_chunk(strategy="vector_rerank"), _chunk(strategy="fact_store"), _chunk(strategy="web_search")]
        ctx = _make_ctx()
        _finalize_response(ctx, "answer", chunks, "corpus_only", "rag", None)
        strategies = {c["filler_strategy"] for c in ctx.rag_chunks}
        assert strategies == {"vector_rerank", "fact_store", "web_search"}

    def test_empty_when_no_sources(self):
        ctx = _make_ctx()
        _finalize_response(ctx, "answer", [], "no_sources", "rag", None)
        assert ctx.rag_chunks == []


class TestToolOutputsGroupedByTool:
    def test_non_rag_tool_output_captured(self):
        ctx = _make_ctx(react_tool_results=[
            {"tool": "appeals_find_carc", "success": True, "result": '{"carc": "22"}'},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_find_carc", None)
        assert ctx.tool_outputs["appeals_find_carc"] == [
            {"success": True, "result": '{"carc": "22"}', "result_summary": None},
        ]

    def test_rag_excluded_lives_in_rag_chunks_instead(self):
        ctx = _make_ctx(react_tool_results=[
            {"tool": "rag", "success": True, "result": "[1] Doc A\ntext"},
            {"tool": "appeals_find_carc", "success": True, "result": "{}"},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_find_carc", None)
        assert "rag" not in ctx.tool_outputs
        assert "appeals_find_carc" in ctx.tool_outputs

    def test_repeated_calls_to_same_tool_all_preserved_not_overwritten(self):
        """The exact live regression this schema fixes: appeals_get_playbook
        called 7x in the COB trace -- a single-value dict would keep only
        the last call and lose the other 6."""
        ctx = _make_ctx(react_tool_results=[
            {"tool": "appeals_get_playbook", "success": True, "result": '{"attempt": 1}'},
            {"tool": "appeals_get_playbook", "success": False, "result": '{"attempt": 2}'},
            {"tool": "appeals_get_playbook", "success": True, "result": '{"attempt": 3}'},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_get_playbook", None)
        assert len(ctx.tool_outputs["appeals_get_playbook"]) == 3
        assert [o["result"] for o in ctx.tool_outputs["appeals_get_playbook"]] == [
            '{"attempt": 1}', '{"attempt": 2}', '{"attempt": 3}',
        ]

    def test_multiple_distinct_tools_each_get_their_own_key(self):
        ctx = _make_ctx(react_tool_results=[
            {"tool": "appeals_find_carc", "success": True, "result": "{}"},
            {"tool": "appeals_get_playbook", "success": True, "result": "{}"},
            {"tool": "lookup_authoritative_sources", "success": True, "result": "{}"},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_find_carc", None)
        assert set(ctx.tool_outputs.keys()) == {
            "appeals_find_carc", "appeals_get_playbook", "lookup_authoritative_sources",
        }

    def test_no_tool_outputs_attribute_when_only_rag_called(self):
        ctx = _make_ctx(react_tool_results=[
            {"tool": "rag", "success": True, "result": "[1] Doc A\ntext"},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "rag", None)
        assert getattr(ctx, "tool_outputs", None) is None

    def test_includes_seed_tool_results_too(self):
        """Mirrors tool_section_hints' own behavior -- seed_tool_results
        (carried context from an earlier turn) count the same as this
        turn's react_tool_results."""
        ctx = _make_ctx(
            react_tool_results=[{"tool": "appeals_find_carc", "success": True, "result": "{}"}],
            seed_tool_results=[{"tool": "appeals_get_playbook", "success": True, "result": "{}"}],
        )
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_find_carc", None)
        assert set(ctx.tool_outputs.keys()) == {"appeals_find_carc", "appeals_get_playbook"}
