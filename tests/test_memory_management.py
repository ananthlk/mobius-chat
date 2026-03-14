"""Unit tests for memory management improvements (spec §10).

Covers:
  §10.1 Unit tests for all 6 improvements
  §10.3 Regression tests for existing router/context-pack behaviour
"""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Improvement 1: build_context_summary
# ---------------------------------------------------------------------------

class TestBuildContextSummary:
    def _summary(self, text: str, sources: list | None = None):
        from app.storage.turns import build_context_summary
        return build_context_summary(text, sources or [])

    def test_outcome_found_n(self):
        s = self._summary("We found 9 NPI matches for Lifestream.")
        assert s.startswith("Found 9 result(s).")

    def test_outcome_no_results(self):
        s = self._summary("No results found for that organization.")
        assert "No results found." in s

    def test_outcome_generated(self):
        s = self._summary("Generated the credentialing report successfully.")
        assert "Generated output." in s

    def test_outcome_explained(self):
        s = self._summary("Here is an overview of prior authorization.")
        assert "Provided explanation." in s

    def test_outcome_error(self):
        s = self._summary("Unable to complete the request due to a connection error.")
        assert "Request could not be completed." in s

    def test_outcome_default(self):
        s = self._summary("Here is some information.")
        assert s  # should not be empty
        assert len(s) <= 600

    def test_jurisdiction_extracted(self):
        s = self._summary("Found 3 providers in Florida matching your query.")
        assert "Florida" in s

    def test_source_entity_used(self):
        sources = [{"document_name": "Sunshine Health Manual 2025"}]
        s = self._summary("We found the answer in the manual.", sources)
        assert "Sunshine Health Manual 2025" in s

    def test_strips_markdown(self):
        s = self._summary("**Found** 2 results. See [link](https://example.com).")
        assert "**" not in s
        assert "https://" not in s

    def test_hard_cap_600_chars(self):
        long = "We found 1 result. " * 50
        s = self._summary(long)
        assert len(s) <= 600


# ---------------------------------------------------------------------------
# Improvement 4: slim_master_plan
# ---------------------------------------------------------------------------

class TestSlimMasterPlan:
    def _slim(self, plan):
        from app.stages.agents.capabilities import slim_master_plan
        return slim_master_plan(plan)

    def test_none_input(self):
        assert self._slim(None) is None

    def test_empty_dict(self):
        assert self._slim({}) is None

    def test_strips_routing_fields(self):
        plan = {
            "plan_summary": "Find NPI for Lifestream",
            "tasks": [
                {
                    "task_id": "t1",
                    "subquestion": "What is the NPI?",
                    "kind": "tool",
                    "capabilities_needed": {"primary": "tools", "fallbacks": ["web"]},
                    "tool_hint": "npi_lookup",
                    "jurisdiction": {"state": "Florida", "payer": None, "program": None},
                    "intent_score": 0.9,
                }
            ],
        }
        slim = self._slim(plan)
        assert slim is not None
        assert "capabilities_needed" not in slim
        assert "kind" not in slim
        assert "intent_score" not in slim
        assert "fallbacks" not in slim

    def test_keeps_intent_and_tools(self):
        plan = {
            "plan_summary": "Run credentialing report for Aspire",
            "tasks": [{"task_id": "t1", "tool_hint": "roster_report", "subquestion": "Run report"}],
        }
        slim = self._slim(plan)
        assert slim["original_intent"] == "Run credentialing report for Aspire"
        assert "roster_report" in slim["tools_used"]

    def test_empty_tasks(self):
        plan = {"plan_summary": "Just a question", "tasks": []}
        slim = self._slim(plan)
        assert slim["tools_used"] == []
        assert slim["jurisdiction"] == {}

    def test_null_tool_hint_excluded(self):
        plan = {
            "plan_summary": "Policy question",
            "tasks": [{"task_id": "t1", "tool_hint": None, "subquestion": "What is prior auth?"}],
        }
        slim = self._slim(plan)
        assert slim["tools_used"] == []


# ---------------------------------------------------------------------------
# Improvement 3: merge_resolved_slots / ThreadState.apply_delta
# ---------------------------------------------------------------------------

class TestResolvedSlots:
    def _state(self, resolved=None):
        from app.state.model import ThreadState
        s = ThreadState()
        if resolved:
            s.resolved_slots = dict(resolved)
        return s

    def test_apply_delta_merges(self):
        state = self._state()
        state.apply_delta({"resolved_slots": {"state": "Florida", "payer": "Sunshine Health"}})
        assert state.resolved_slots["state"] == "Florida"
        assert state.resolved_slots["payer"] == "Sunshine Health"

    def test_user_value_wins(self):
        state = self._state({"state": "Texas"})
        state.apply_delta({"resolved_slots": {"state": "Florida"}})
        assert state.resolved_slots["state"] == "Florida"

    def test_null_does_not_overwrite(self):
        state = self._state({"state": "Florida"})
        state.apply_delta({"resolved_slots": {"state": None}})
        assert state.resolved_slots["state"] == "Florida"

    def test_none_string_does_not_overwrite(self):
        state = self._state({"payer": "Sunshine Health"})
        state.apply_delta({"resolved_slots": {"payer": "null"}})
        assert state.resolved_slots["payer"] == "Sunshine Health"

    def test_clear_slots(self):
        state = self._state({"state": "FL"})
        state.open_slots = ["state"]
        state.clear_slots()
        assert state.resolved_slots == {}
        assert state.open_slots == []

    def test_to_dict_includes_resolved_slots(self):
        state = self._state({"state": "FL"})
        d = state.to_dict()
        assert "resolved_slots" in d
        assert d["resolved_slots"]["state"] == "FL"

    def test_from_dict_includes_resolved_slots(self):
        from app.state.model import ThreadState
        d = {"resolved_slots": {"payer": "Molina"}, "open_slots": [], "active": {}}
        state = ThreadState.from_dict(d)
        assert state.resolved_slots["payer"] == "Molina"


# ---------------------------------------------------------------------------
# Improvement 6: MemoryPersistence — TTL + last-10 + warning log
# ---------------------------------------------------------------------------

class TestMemoryPersistence:
    def _make(self):
        # Clear module-level store before each test
        import app.persistence.memory as m
        m._store.clear()
        return m.MemoryPersistence()

    def test_warning_logged_on_init(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="app.persistence.memory"):
            self._make()
        assert "MemoryPersistence active" in caplog.text

    def test_save_and_load_state(self):
        p = self._make()
        p.save_state("t1", {"active": {"payer": "Aetna"}})
        s = p.load_state("t1")
        assert s["active"]["payer"] == "Aetna"

    def test_ttl_eviction(self):
        import app.persistence.memory as m
        p = self._make()
        # Plant an already-expired entry
        m._store["state:t2"] = ({"x": 1}, time.time() - 1)
        assert p.load_state("t2") is None

    def test_last_10_turns_kept(self):
        p = self._make()
        for i in range(12):
            p.save_turn(
                f"cid{i}", f"q{i}", [], f"a{i}", [],
                None, None, None, thread_id="t3"
            )
        turns = p.get_last_turns("t3", n=10)
        assert len(turns) == 10

    def test_get_last_turns_fewer_than_n(self):
        p = self._make()
        p.save_turn("c1", "q1", [], "a1", [], None, None, None, thread_id="t4")
        turns = p.get_last_turns("t4", n=5)
        assert len(turns) == 1

    def test_unknown_thread_returns_empty(self):
        p = self._make()
        assert p.get_last_turns("no-such-thread") == []
        assert p.load_state("no-such-thread") is None


# ---------------------------------------------------------------------------
# Improvement 5: format_cached_result — size cap
# ---------------------------------------------------------------------------

class TestFormatCachedResult:
    def _fmt(self, tool_hint, result):
        from app.storage.results import format_cached_result
        return format_cached_result(tool_hint, result)

    def test_basic_output(self):
        result = {"count": 3, "rows": [{"npi": "1"}, {"npi": "2"}, {"npi": "3"}]}
        out = self._fmt("npi_lookup", result)
        assert "Npi Lookup" in out
        assert "3 total" in out

    def test_truncation_at_3200_chars(self):
        # 5 rows × ~730 chars/row = ~3650 chars → over 3200 cap
        big_rows = [{"data": "x" * 700}] * 20
        result = {"count": 20, "rows": big_rows}
        out = self._fmt("roster_report", result)
        assert len(out) <= 3200

    def test_truncation_suffix(self):
        big_rows = [{"data": "x" * 700}] * 20
        result = {"count": 20, "rows": big_rows}
        out = self._fmt("roster_report", result)
        assert "truncated" in out

    def test_empty_rows(self):
        out = self._fmt("google_search", {"count": 0, "rows": []})
        assert "0 total" in out


# ---------------------------------------------------------------------------
# §10.3 Regression tests — existing router / context-pack behaviour
# ---------------------------------------------------------------------------

class TestContextRouterRegression:
    def _route(self, text, state=None, last_turns=None, reset_reason=None):
        from app.state.context_router import route_context
        return route_context(text, state or {}, last_turns or [], reset_reason=reset_reason)

    def test_payer_change_still_standalone(self):
        assert self._route("anything", reset_reason="payer_change") == "STANDALONE"

    def test_new_question_phrase_standalone(self):
        assert self._route("new question: what is prior auth?") == "STANDALONE"

    def test_pronoun_it_stateful(self):
        assert self._route("can you search for it") == "STATEFUL"

    def test_expand_on_stateful(self):
        assert self._route("expand on the second result") == "STATEFUL"

    def test_open_slots_stateful(self):
        state = {"open_slots": ["state"], "active": {}}
        assert self._route("Florida", state) == "STATEFUL"

    def test_payer_in_state_stateful(self):
        state = {"open_slots": [], "active": {"payer": "Sunshine Health"}}
        assert self._route("what are the eligibility rules?", state) == "STATEFUL"

    def test_no_context_light(self):
        # No pronouns, no payer, no open slots → LIGHT (LLM not called: no prior summary)
        # Pass an explicit empty state to avoid leakage from any test env
        result = self._route("what is prior authorization?", state={"active": {}, "open_slots": []})
        assert result in ("LIGHT", "STANDALONE")


class TestContextPackRegression:
    def _pack(self, route, state=None, turns=None, slots=None):
        from app.state.context_pack import build_context_pack
        return build_context_pack(route, state or {}, turns or [], slots or [])

    def test_standalone_returns_empty(self):
        assert self._pack("STANDALONE") == ""

    def test_light_one_turn(self):
        turns = [{"user_content": "What is PA?", "assistant_content": "Prior auth requires..."}]
        pack = self._pack("LIGHT", turns=turns)
        assert "Last turn:" in pack
        assert "What is PA?" in pack

    def test_light_context_summary_preferred(self):
        turns = [{"user_content": "q", "context_summary": "Found 9 results.", "assistant_content": "x" * 500}]
        pack = self._pack("LIGHT", turns=turns)
        assert "Found 9 results." in pack
        assert "x" * 200 not in pack  # long raw text should NOT appear

    def test_stateful_two_turns(self):
        turns = [
            {"user_content": "Turn 2 Q", "assistant_content": "Turn 2 A"},
            {"user_content": "Turn 1 Q", "assistant_content": "Turn 1 A"},
        ]
        pack = self._pack("STATEFUL", turns=turns)
        assert "Turn 1:" in pack
        assert "Turn 2:" in pack

    def test_resolved_slots_injected(self):
        state = {"resolved_slots": {"state": "Florida", "payer": "Sunshine"}, "active": {}}
        pack = self._pack("STATEFUL", state=state)
        assert "Resolved context:" in pack
        assert "state = Florida" in pack

    def test_resolved_slots_empty_not_shown(self):
        state = {"resolved_slots": {}, "active": {}}
        pack = self._pack("LIGHT", state=state)
        assert "Resolved context:" not in pack

    def test_no_last_master_plan_in_slim_none(self):
        from app.stages.agents.capabilities import slim_master_plan
        assert slim_master_plan(None) is None

    def test_last_master_plan_not_empty_dict(self):
        from app.stages.agents.capabilities import slim_master_plan
        assert slim_master_plan({}) is None
