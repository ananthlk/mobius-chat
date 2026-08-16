"""ctx.rag_chunks + ctx.tool_outputs + ctx.reasoning_trace (2026-08-07,
Task #58, schema approved by coordinator, Ananth's ruling on envelope
taxonomy + "factory model" inline enrichment).

ctx.rag_chunks: unified pool of every rag chunk retrieved this turn, all
filler arms merged, renamed to the approved field contract (authority ->
authority_level, confidence_label -> confidence, rerank_score -> score).
Full pool, uncapped -- integrate.py's top-N slicing is a downstream
consumption choice, not baked in here.

ctx.tool_outputs: typed, grouped by tool FAMILY (appeals: letter/rules/
playbook/validation; analytics; authoritative_sources) -- not a flat
dict[tool_name, raw-string]. rag excluded (that's rag_chunks' job). A
call whose section_hint already carries equivalent data is skipped here
(no-duplicate rule) so the enricher doesn't see the same content twice.

ctx.reasoning_trace: alias of ctx.react_trace_rounds, now carrying
tool/inputs/enrichment (learned/running_answer/gaps) per round -- the
gap Chat Master identified: evidence_review already computed real
per-round interpretation, it just wasn't persisted past the current
round or paired to the tool call that produced it.

Same test pattern as test_section_hint_pipeline.py: SimpleNamespace ctx,
call _finalize_response directly, assert on the ctx fields it sets.
"""
from __future__ import annotations

import json
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
    def test_rag_chunks_renamed_field_contract(self):
        chunks = [_chunk(doc="Doc A", authority="contract_source_of_truth", rerank=0.9, strategy="vector_rerank")]
        ctx = _make_ctx()
        _finalize_response(ctx, "answer", chunks, "corpus_only", "rag", None)
        c = ctx.rag_chunks[0]
        assert c["authority_level"] == "contract_source_of_truth"
        assert c["confidence"] == "high"
        assert c["score"] == 0.9
        assert c["filler_strategy"] == "vector_rerank"
        assert c["document_name"] == "Doc A"

    def test_rag_chunks_not_capped_to_seven(self):
        """The whole point: integrate.py's source_texts caps at 7 for
        prompt-size reasons, but ctx.rag_chunks itself must carry
        everything -- capping is a downstream choice, not baked in here."""
        chunks = [_chunk(doc=f"Doc {i}") for i in range(12)]
        ctx = _make_ctx()
        _finalize_response(ctx, "answer", chunks, "corpus_only", "rag", None)
        assert len(ctx.rag_chunks) == 12

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


class TestToolOutputsTypedByFamily:
    def test_appeals_rules_from_lookup_rules(self):
        rule = {"rule_id": "COB.R001", "rule_name": "Payor of Last Resort", "rule_statement": "...",
                "appeal_argument": "...", "authority": {"state": None, "federal": None, "clinical": None}}
        ctx = _make_ctx(react_tool_results=[
            {"tool": "appeals_lookup_rules", "success": True, "result": json.dumps({"carc": 22, "rules": [rule]})},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_lookup_rules", None)
        assert ctx.tool_outputs["appeals"]["rules"] == [rule]

    def test_appeals_rules_unified_from_find_carc_nested_matches(self):
        """appeals_find_carc nests rules per-candidate (matches[i].rules)
        -- must still land in the SAME unified rules list as
        appeals_lookup_rules' top-level rules."""
        rule = {"rule_id": "COB.R002", "rule_name": "x", "rule_statement": "y", "appeal_argument": "z", "authority": {}}
        ctx = _make_ctx(react_tool_results=[
            {"tool": "appeals_find_carc", "success": True,
             "result": json.dumps({"matches": [{"carc": 22, "rules": [rule]}], "top_carc": 22})},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_find_carc", None)
        assert ctx.tool_outputs["appeals"]["rules"] == [rule]

    def test_appeals_letter_sourced_from_recital_not_raw_result(self):
        """appeals_assemble_letter's raw result is plain letter TEXT, not
        JSON, when successful -- must come from ctx.recital (already the
        verbatim text the dispatch handler set), not a JSON re-parse."""
        ctx = _make_ctx(
            react_tool_results=[{"tool": "appeals_assemble_letter", "success": True, "result": "Dear Sunshine Health,..."}],
            recital={"verbatim": True, "text": "Dear Sunshine Health,..."},
        )
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_assemble_letter", None)
        assert ctx.tool_outputs["appeals"]["letter"]["verbatim"] == "Dear Sunshine Health,..."

    def test_appeals_playbook_usable_call_wins_over_empty_ones(self):
        """The exact live COB trace: 7 appeals_get_playbook calls, most
        found-but-empty (the zero-result fix's own definition), one
        (if any) with real data. The usable one must win regardless of
        call order."""
        empty = json.dumps({"found": True})
        usable = json.dumps({"found": True, "deadline_appeal_days": 90, "submission_method": "provider portal"})
        ctx = _make_ctx(react_tool_results=[
            {"tool": "appeals_get_playbook", "success": True, "result": empty},
            {"tool": "appeals_get_playbook", "success": True, "result": usable},
            {"tool": "appeals_get_playbook", "success": True, "result": empty},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_get_playbook", None)
        assert ctx.tool_outputs["appeals"]["playbook"]["deadline_appeal_days"] == 90

    def test_appeals_playbook_not_found_produces_no_playbook_key(self):
        ctx = _make_ctx(react_tool_results=[
            {"tool": "appeals_get_playbook", "success": True, "result": json.dumps({"found": False, "message": "no playbook"})},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_get_playbook", None)
        assert "playbook" not in getattr(ctx, "tool_outputs", {}).get("appeals", {})

    def test_appeals_validation_is_a_fourth_key_not_dropped(self):
        """appeals_validate_claim doesn't fit letter/rules/playbook --
        must not be silently discarded (that would contradict the whole
        point of this task: integrator needs ALL collected information)."""
        ctx = _make_ctx(react_tool_results=[
            {"tool": "appeals_validate_claim", "success": True, "result": json.dumps({"action": "appeal", "confidence": "high"})},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_validate_claim", None)
        assert ctx.tool_outputs["appeals"]["validation"] == {"action": "appeal", "confidence": "high"}

    def test_authoritative_sources_captured(self):
        ctx = _make_ctx(react_tool_results=[
            {"tool": "lookup_authoritative_sources", "success": True,
             "result": json.dumps({"sources": [{"url": "https://sunshinehealth.com/manual.pdf"}]})},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "lookup_authoritative_sources", None)
        assert ctx.tool_outputs["authoritative_sources"] == [{"url": "https://sunshinehealth.com/manual.pdf"}]

    def test_rag_excluded_lives_in_rag_chunks_instead(self):
        ctx = _make_ctx(react_tool_results=[
            {"tool": "rag", "success": True, "result": "[1] Doc A\ntext"},
            {"tool": "appeals_lookup_rules", "success": True, "result": json.dumps({"rules": []})},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_lookup_rules", None)
        assert "rag" not in getattr(ctx, "tool_outputs", {})

    def test_no_tool_outputs_attribute_when_only_rag_called(self):
        ctx = _make_ctx(react_tool_results=[
            {"tool": "rag", "success": True, "result": "[1] Doc A\ntext"},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "rag", None)
        assert getattr(ctx, "tool_outputs", None) is None

    def test_no_duplicate_when_section_hint_already_present(self):
        """A call whose section_hint already carries this data must be
        skipped from tool_outputs -- the enricher shouldn't see the same
        rules content twice through two different channels."""
        hint = {"section_format": "appeals_rules", "label": "Appeal rules", "data": {"rules": ["already-hinted"]}}
        ctx = _make_ctx(react_tool_results=[
            {"tool": "appeals_lookup_rules", "success": True, "result": json.dumps({"rules": [{"rule_id": "X"}]}),
             "section_hint": hint},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_lookup_rules", None)
        assert "appeals" not in getattr(ctx, "tool_outputs", {})

    def test_failed_calls_excluded(self):
        ctx = _make_ctx(react_tool_results=[
            {"tool": "appeals_lookup_rules", "success": False, "result": json.dumps({"rules": [{"rule_id": "X"}]})},
        ])
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_lookup_rules", None)
        assert getattr(ctx, "tool_outputs", None) is None

    def test_includes_seed_tool_results_too(self):
        """Mirrors tool_section_hints' own behavior -- seed_tool_results
        (carried context from an earlier turn) count the same as this
        turn's react_tool_results."""
        ctx = _make_ctx(
            react_tool_results=[{"tool": "appeals_lookup_rules", "success": True, "result": json.dumps({"rules": [{"rule_id": "A"}]})}],
            seed_tool_results=[{"tool": "appeals_validate_claim", "success": True, "result": json.dumps({"action": "appeal"})}],
        )
        _finalize_response(ctx, "answer", [], "no_sources", "appeals_lookup_rules", None)
        assert ctx.tool_outputs["appeals"]["rules"] == [{"rule_id": "A"}]
        assert ctx.tool_outputs["appeals"]["validation"] == {"action": "appeal"}


class TestReasoningTraceAlias:
    def test_reasoning_trace_mirrors_react_trace_rounds(self):
        ctx = _make_ctx(react_trace_rounds=[{"round": 1, "directive": "explore"}])
        _finalize_response(ctx, "answer", [], "no_sources", "rag", None)
        assert ctx.reasoning_trace == [{"round": 1, "directive": "explore"}]

    def test_absent_when_no_rounds(self):
        ctx = _make_ctx(react_trace_rounds=[])
        _finalize_response(ctx, "answer", [], "no_sources", "rag", None)
        assert getattr(ctx, "reasoning_trace", None) is None


class TestEnrichmentPairedToRoundEndToEnd:
    """Confirms the actual mutation inside run_react's round loop, not
    just the finalize-time alias: each round's ctx.react_trace_rounds
    entry gets tool/inputs/enrichment written onto it once that round's
    decision (including evidence_review) is parsed -- not a separate,
    disconnected structure."""

    def test_evidence_review_lands_on_the_same_round_it_was_reasoned_in(self):
        from unittest.mock import patch
        from app.pipeline.context import PipelineContext
        from app.pipeline.react_loop import run_react

        ctx = PipelineContext(correlation_id="react-enrich", thread_id=None, message="timely filing deadline?")
        ctx.merged_state = {}
        ctx.last_turns = []
        ctx.effective_message = ctx.message

        reason_count = 0

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            nonlocal reason_count
            reason_count += 1
            if reason_count == 1:
                return '{"thought": "Try rag first.", "tool": "rag", "inputs": {"query": "timely filing"}, "is_complete": false}'
            return (
                '{"thought": "Found it.", '
                '"evidence_review": {"keep": [1], "running_answer": "180 days", "gaps_closed": ["timely filing window"], "gaps_open": []}, '
                '"tool": null, "inputs": {}, "is_complete": true, '
                '"answer": "180 days.", "sources": [], "confidence": "high"}'
            )

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
                mock_execute.return_value = {
                    "tool": "rag", "success": True,
                    "result": "[1] Doc A\n180 calendar days from date of service.",
                    "signal": "corpus_only", "sources": [], "usage": None,
                }
                run_react(ctx, emitter=None)

        assert ctx.final_message == "180 days."
        # Round 2 is the finalizing round -- its trace entry must carry
        # the enrichment that was reasoned in that same round, not round 1's.
        round_2_entries = [r for r in ctx.reasoning_trace if r.get("round") == 2]
        assert round_2_entries, "expected a trace entry for round 2"
        entry = round_2_entries[0]
        enrichment = entry["enrichment"]
        assert enrichment["running_answer"] == "180 days"
        assert enrichment["learned"] == "Found it."
        assert enrichment["gaps_closed"] == ["timely filing window"]
        assert enrichment["gaps_open"] == []
        # raw_result_ref is a pointer into ctx.tool_outputs, not a copy --
        # this round enriched the 1st (only) rag call this turn.
        assert entry["raw_result_ref"] == {"tool_name": "rag", "call_index": 1}


class TestGapsClosedFallbackSynthesis:
    """Task #95 (2026-08-12, Chat Master directive): a round-1 fast-path
    completion (Rule 8/11 -- "Recent conversation" already answers the
    question, is_complete=true with no tool call) never populates
    evidence_review at all, so #90's gaps_closed index gets nothing even
    when the turn genuinely resolved something. _finalize_response
    synthesizes one gaps_closed entry (= ctx.message) on the terminal
    round when the whole trace closed with zero gaps_closed/gaps_open
    anywhere and the answer is a real, non-hedged completion."""

    def test_fires_on_round_1_fast_path_no_enrichment_at_all(self):
        ctx = _make_ctx(
            react_trace_rounds=[{"round": 1, "tool": None}],
            message="What is Humana's timely filing deadline?",
        )
        _finalize_response(ctx, "**Humana's deadline is 180 days.**", [], "corpus_only", None, None)
        entry = ctx.reasoning_trace[0]
        assert entry["enrichment"]["gaps_closed"] == ["What is Humana's timely filing deadline?"]

    def test_fires_when_enrichment_present_but_gaps_both_empty(self):
        ctx = _make_ctx(
            react_trace_rounds=[{
                "round": 1, "tool": None,
                "enrichment": {"learned": "reused prior answer", "running_answer": "", "gaps_closed": [], "gaps_open": []},
            }],
            message="Aetna's deadline?",
        )
        _finalize_response(ctx, "**Aetna's deadline is 180 days.**", [], "corpus_only", None, None)
        assert ctx.reasoning_trace[0]["enrichment"]["gaps_closed"] == ["Aetna's deadline?"]

    def test_excluded_when_gaps_open_present_anywhere(self):
        """T2/Humana's actual case (Task #95's corrected finding): a
        genuine 'not found' turn with gaps_open recorded must NOT be
        synthesized into a false resolution."""
        ctx = _make_ctx(
            react_trace_rounds=[
                {"round": 1, "tool": "rag"},
                {"round": 2, "tool": None, "enrichment": {
                    "learned": "not found", "running_answer": "not found", "gaps_closed": [], "gaps_open": ["Humana deadline"],
                }},
            ],
            message="Humana's deadline?",
        )
        _finalize_response(ctx, "Not found in our materials.", [], "no_sources", None, None)
        assert ctx.reasoning_trace[1]["enrichment"]["gaps_closed"] == []

    def test_excluded_when_gaps_closed_already_present_no_duplicate(self):
        ctx = _make_ctx(
            react_trace_rounds=[
                {"round": 1, "tool": "rag"},
                {"round": 2, "tool": None, "enrichment": {
                    "learned": "found it", "running_answer": "180 days", "gaps_closed": ["deadline"], "gaps_open": [],
                }},
            ],
            message="Sunshine's deadline?",
        )
        _finalize_response(ctx, "180 days.", [], "corpus_only", None, None)
        assert ctx.reasoning_trace[1]["enrichment"]["gaps_closed"] == ["deadline"]

    def test_excluded_when_answer_text_reads_as_hedge(self):
        """Backstop for a round-1 fast path that itself produces an
        honest non-answer -- no evidence_review ever ran, so the
        gaps_open check alone can't catch it."""
        ctx = _make_ctx(
            react_trace_rounds=[{"round": 1, "tool": None}],
            message="Molina's deadline?",
        )
        _finalize_response(ctx, "That was not found in our materials.", [], "no_sources", None, None)
        assert "gaps_closed" not in (ctx.reasoning_trace[0].get("enrichment") or {})

    def test_excluded_when_terminal_round_made_a_tool_call(self):
        """Defensive: a terminal round with tool set means is_complete
        was false (schema's tool-call/final-answer shapes are mutually
        exclusive) -- should never happen in practice by the time
        _finalize_response runs, but the guard must not fire on it."""
        ctx = _make_ctx(
            react_trace_rounds=[{"round": 1, "tool": "rag"}],
            message="Aetna's deadline?",
        )
        _finalize_response(ctx, "some answer", [], "corpus_only", "rag", None)
        assert "gaps_closed" not in (ctx.reasoning_trace[0].get("enrichment") or {})

    def test_excluded_when_answer_empty(self):
        ctx = _make_ctx(react_trace_rounds=[{"round": 1, "tool": None}], message="Aetna's deadline?")
        _finalize_response(ctx, "", [], "no_sources", None, None)
        assert "gaps_closed" not in (ctx.reasoning_trace[0].get("enrichment") or {})

    def test_excluded_when_message_empty(self):
        ctx = _make_ctx(react_trace_rounds=[{"round": 1, "tool": None}], message="")
        _finalize_response(ctx, "some answer", [], "corpus_only", None, None)
        assert "gaps_closed" not in (ctx.reasoning_trace[0].get("enrichment") or {})

    def test_no_crash_on_empty_react_trace_rounds(self):
        ctx = _make_ctx(react_trace_rounds=[], message="Aetna's deadline?")
        _finalize_response(ctx, "some answer", [], "corpus_only", None, None)
        assert getattr(ctx, "reasoning_trace", None) is None


class TestCompletionGateCritic:
    """Task #104 (2026-08-16, docs/REACT_COMPLETION_CRITIC_DESIGN.md):
    end-to-end loop-back behavior. The completion-gate critic intercepts
    an is_complete=true round in agentic mode; when unsatisfied, it
    extends the round budget (sharing the groundedness floor's own
    extension-round ledger) and injects gaps/next_query for the next
    round's context -- same trigger point as the Product Promise
    groundedness floor, running before it."""

    def test_unsatisfied_verdict_extends_and_loops_back(self, monkeypatch):
        from unittest.mock import patch
        from app.pipeline.context import PipelineContext
        from app.pipeline.react_loop import run_react

        monkeypatch.setenv("MOBIUS_PRODUCT_PROMISE_ENABLED", "1")

        ctx = PipelineContext(correlation_id="cc-1", thread_id=None, message="CPT codes for BH, SUD, and FQHC?")
        ctx.chat_mode = "agentic"
        ctx.merged_state = {}
        ctx.last_turns = []
        ctx.effective_message = ctx.message

        call_log: list[str] = []

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            call_log.append(stage)
            if stage == "react_completion_critic":
                n = call_log.count("react_completion_critic")
                if n == 1:
                    return (
                        '{"satisfied": false, "uncovered": ["SUD limits", "FQHC codes"], '
                        '"suggested_next_query": "FQHC behavioral health CPT codes"}'
                    )
                return '{"satisfied": true, "uncovered": [], "suggested_next_query": ""}'
            if stage == "critique":
                return '{"grounded": true, "issues": []}'
            # planner rounds (react_1, react_2, react_3, ...)
            n = sum(1 for s in call_log if s.startswith("react_") and s != "react_completion_critic")
            if n == 1:
                return (
                    '{"thought": "Search first.", "tool": "rag", '
                    '"inputs": {"query": "BH SUD FQHC CPT codes"}, "is_complete": false}'
                )
            if n == 2:
                return (
                    '{"thought": "Found BH code.", '
                    '"evidence_review": {"keep": [1], "running_answer": "BH: 90834.", '
                    '"gaps_closed": ["BH code"], "gaps_open": ["SUD code", "FQHC code"]}, '
                    '"tool": null, "inputs": {}, "is_complete": true, '
                    '"answer": "BH: 90834.", "sources": [], "confidence": "high"}'
                )
            return (
                '{"thought": "Found remaining codes.", '
                '"evidence_review": {"keep": [1], '
                '"running_answer": "BH: 90834. SUD: 90837. FQHC: modifier codes.", '
                '"gaps_closed": ["SUD code", "FQHC code"], "gaps_open": []}, '
                '"tool": null, "inputs": {}, "is_complete": true, '
                '"answer": "BH: 90834. SUD: 90837. FQHC: modifier codes.", "sources": [], "confidence": "high"}'
            )

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
                mock_execute.return_value = {
                    "tool": "rag", "success": True,
                    "result": "[1] Doc A\nBH: 90834. SUD: 90837. FQHC: modifier codes.",
                    "signal": "corpus_only", "sources": [], "usage": None,
                }
                run_react(ctx, emitter=None)

        assert ctx.final_message == "BH: 90834. SUD: 90837. FQHC: modifier codes."
        assert call_log.count("react_completion_critic") == 2
        # The extension actually happened -- three planner rounds ran
        # (round 1 tool call, round 2 flagged incomplete by the critic,
        # round 3 completes after the critic's forced extension).
        planner_calls = [s for s in call_log if s.startswith("react_") and s != "react_completion_critic"]
        assert len(planner_calls) == 3
        assert ctx.completion_critic_ran is True

    def test_satisfied_verdict_finalizes_without_extension(self, monkeypatch):
        """Sanity check on the other branch: a satisfied verdict on the
        first completion attempt must NOT trigger an extension -- no
        false loop-back on an answer that already covers everything."""
        from unittest.mock import patch
        from app.pipeline.context import PipelineContext
        from app.pipeline.react_loop import run_react

        monkeypatch.setenv("MOBIUS_PRODUCT_PROMISE_ENABLED", "1")

        ctx = PipelineContext(correlation_id="cc-2", thread_id=None, message="Aetna's timely filing deadline?")
        ctx.chat_mode = "agentic"
        ctx.merged_state = {}
        ctx.last_turns = []
        ctx.effective_message = ctx.message

        call_log: list[str] = []

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            call_log.append(stage)
            if stage == "react_completion_critic":
                return '{"satisfied": true, "uncovered": [], "suggested_next_query": ""}'
            if stage == "critique":
                return '{"grounded": true, "issues": []}'
            n = sum(1 for s in call_log if s.startswith("react_") and s != "react_completion_critic")
            if n == 1:
                return '{"thought": "Search.", "tool": "rag", "inputs": {"query": "Aetna deadline"}, "is_complete": false}'
            return (
                '{"thought": "Found it.", '
                '"evidence_review": {"keep": [1], "running_answer": "180 days.", '
                '"gaps_closed": ["deadline"], "gaps_open": []}, '
                '"tool": null, "inputs": {}, "is_complete": true, '
                '"answer": "180 days.", "sources": [], "confidence": "high"}'
            )

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            with patch("app.pipeline.react_loop._execute_tool") as mock_execute:
                mock_execute.return_value = {
                    "tool": "rag", "success": True,
                    "result": "[1] Doc A\n180 days.",
                    "signal": "corpus_only", "sources": [], "usage": None,
                }
                run_react(ctx, emitter=None)

        assert ctx.final_message == "180 days."
        assert call_log.count("react_completion_critic") == 1
        planner_calls = [s for s in call_log if s.startswith("react_") and s != "react_completion_critic"]
        assert len(planner_calls) == 2  # no extension round

    def test_skipped_outside_agentic_mode(self, monkeypatch):
        """Copilot mode must never pay for this call -- design §1/§3 is
        explicit: chat.thinking (agentic) only."""
        from unittest.mock import patch
        from app.pipeline.context import PipelineContext
        from app.pipeline.react_loop import run_react

        monkeypatch.setenv("MOBIUS_PRODUCT_PROMISE_ENABLED", "1")

        ctx = PipelineContext(correlation_id="cc-3", thread_id=None, message="Aetna's timely filing deadline?")
        ctx.chat_mode = "copilot"
        ctx.merged_state = {}
        ctx.last_turns = []
        ctx.effective_message = ctx.message

        call_log: list[str] = []

        def fake_llm(system, user, max_tokens=800, ctx=None, stage="planner", **kwargs):
            call_log.append(stage)
            if stage == "critique":
                return '{"grounded": true, "issues": []}'
            return (
                '{"thought": "Found it.", '
                '"evidence_review": {"keep": [1], "running_answer": "180 days.", '
                '"gaps_closed": ["deadline"], "gaps_open": []}, '
                '"tool": null, "inputs": {}, "is_complete": true, '
                '"answer": "180 days.", "sources": [], "confidence": "high"}'
            )

        with patch("app.pipeline.react.critic.critic_enabled", return_value=False), \
             patch("app.pipeline.react_loop._call_llm_json", side_effect=fake_llm):
            run_react(ctx, emitter=None)

        assert "react_completion_critic" not in call_log
        assert ctx.completion_critic_ran is False
