"""Fabrication-on-sparse-corpus hedge (2026-08-07, Task #65, Chat
Master + LLM Agent, live-query finding, cid d288d009).

Live incident: 2 kept chunks, 401 chars total, none containing the
question's key terms (MMA/LTC/Comprehensive). react_draft still stated
specific program details confidently, cited to a chunk ([4]) that
didn't say them. Two distinct failure modes:

(1) Sparse corpus -> confident synthesis, no hedge. Fixed with a
code-computed sparsity signal (react_loop.py's _kept_chunk_stats) that
injects an explicit, unavoidable hedge instruction into the [Evidence
Review] block -- not something react has to self-diagnose.

(2) Hallucinated citation attribution -- a claim cited to a chunk that
doesn't contain it. Fixed with a standing citation-discipline rule
(rule 1c-2), unconditional on evidence sparsity since a fabricated
citation is wrong even on a rich-corpus turn.
"""
from __future__ import annotations

from app.pipeline.context import PipelineContext
from app.pipeline.react.prompts import build_reasoning_context
from app.pipeline.react_loop import _kept_chunk_stats

_THIN_CORPUS = (
    "[1] Provider Directory\nSunshine Health offers various plans.\n\n"
    "[4] General Overview\nMembers can access services through the network."
)

_RICH_CORPUS = (
    "[1] Care Management Guide\n" + ("Comprehensive program covers MMA and LTC members. " * 30) + "\n\n"
    "[2] Provider Manual\n" + ("Eligibility criteria for the Comprehensive program include... " * 20)
)


def _make_ctx() -> PipelineContext:
    ctx = PipelineContext(correlation_id="sparse-test", thread_id=None, message="care management programs?")
    ctx.effective_message = ctx.message
    ctx.merged_state = {}
    ctx.last_turns = []
    ctx.chat_mode = "agentic"
    ctx.thinking_chunks = []
    return ctx


class TestKeptChunkStats:
    def test_counts_and_chars_for_kept_chunks_only(self):
        count, chars = _kept_chunk_stats(_THIN_CORPUS, [1, 4])
        assert count == 2
        assert chars == len("Sunshine Health offers various plans.") + len("Members can access services through the network.")

    def test_header_length_not_counted_as_evidence(self):
        """A chunk with a long document-name header but short text
        shouldn't look substantive just because the header is long."""
        raw = "[1] A Very Long Document Name That Goes On And On And On\nShort."
        count, chars = _kept_chunk_stats(raw, [1])
        assert chars == len("Short.")

    def test_unkept_chunks_excluded(self):
        count, chars = _kept_chunk_stats(_THIN_CORPUS, [1])
        assert count == 1
        assert chars == len("Sunshine Health offers various plans.")

    def test_empty_keep_list_returns_zero(self):
        assert _kept_chunk_stats(_THIN_CORPUS, []) == (0, 0)

    def test_non_chunked_text_returns_zero(self):
        assert _kept_chunk_stats("plain NPPES prose, no headers", [1]) == (0, 0)


class TestSparseEvidenceHedgeInContext:
    def test_thin_evidence_triggers_hedge_instruction(self):
        out = build_reasoning_context(
            _make_ctx(), [], 2,
            evidence_review_latest={
                "running_answer": "Comprehensive program covers MMA and LTC members.",
                "gaps_open": [], "gaps_closed": [],
                "sparse_evidence": True, "kept_chunk_count": 2, "kept_chunk_chars": 401,
            },
        )
        assert "EVIDENCE IS THIN" in out
        assert "2 chunk(s)" in out
        assert "401 chars" in out
        assert "MUST hedge explicitly" in out

    def test_rich_evidence_no_hedge_instruction(self):
        out = build_reasoning_context(
            _make_ctx(), [], 2,
            evidence_review_latest={
                "running_answer": "Comprehensive program covers MMA and LTC members, per the care management guide.",
                "gaps_open": [], "gaps_closed": [],
                "sparse_evidence": False, "kept_chunk_count": 5, "kept_chunk_chars": 2400,
            },
        )
        assert "EVIDENCE IS THIN" not in out

    def test_sparse_flag_absent_when_not_set(self):
        """Legacy/older evidence_review dicts (no sparse_evidence key)
        must not crash and must not spuriously hedge."""
        out = build_reasoning_context(
            _make_ctx(), [], 2,
            evidence_review_latest={"running_answer": "found it", "gaps_open": [], "gaps_closed": []},
        )
        assert "EVIDENCE IS THIN" not in out


class TestSparsityComputedEndToEndInRealRound:
    """Confirms _kept_chunk_stats actually runs inside run_react's round
    loop and lands on ctx._evidence_review_latest -- not just testable
    in isolation."""

    def test_thin_kept_evidence_flagged_after_a_real_round(self):
        from unittest.mock import patch
        from app.pipeline.react_loop import run_react

        ctx = PipelineContext(correlation_id="sparse-e2e", thread_id=None, message="care management programs?")
        ctx.merged_state = {}
        ctx.last_turns = []
        ctx.effective_message = ctx.message

        reason_count = 0

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            nonlocal reason_count
            reason_count += 1
            if reason_count == 1:
                return '{"thought": "search", "tool": "rag", "inputs": {"query": "care programs"}, "is_complete": false}'
            return (
                '{"thought": "thin evidence found", '
                '"evidence_review": {"keep": [1], "running_answer": "Limited info found.", "gaps_closed": [], "gaps_open": ["program details"]}, '
                '"tool": null, "inputs": {}, "is_complete": true, '
                '"answer": "Limited info found.", "sources": [], "confidence": "low"}'
            )

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
                mock_execute.return_value = {
                    "tool": "rag", "success": True,
                    "result": "[1] Provider Directory\nSunshine Health offers various plans.",
                    "signal": "corpus_only", "sources": [], "usage": None,
                }
                run_react(ctx, emitter=None)

        latest = ctx._evidence_review_latest
        assert latest["sparse_evidence"] is True
        assert latest["kept_chunk_count"] == 1
        assert latest["kept_chunk_chars"] == len("Sunshine Health offers various plans.")


class TestCitationDisciplineRule:
    def test_rule_present_in_critical_rules(self):
        from app.pipeline.react.prompts import REACT_CRITICAL_RULES_TEXT
        assert "Citation discipline" in REACT_CRITICAL_RULES_TEXT
        assert "literally" in REACT_CRITICAL_RULES_TEXT.lower()

    def test_rule_applies_regardless_of_evidence_richness(self):
        """The fix must not be framed as sparse-evidence-only -- a
        fabricated citation is wrong on a rich-corpus turn too."""
        from app.pipeline.react.prompts import REACT_CRITICAL_RULES_TEXT
        assert "rich-corpus" in REACT_CRITICAL_RULES_TEXT or "regardless" in REACT_CRITICAL_RULES_TEXT
