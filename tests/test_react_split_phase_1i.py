"""Phase 1i (pass 1) — lock in the react_loop.py split.

The 2026-04-18 audit caught that app/pipeline/react_loop.py had grown to
2,459 LOC — the main.py ratchet was watching the wrong file. Pass 1 of
the split extracts two self-contained clusters into a new package:

  app/pipeline/react/parsing.py      — JSON decision parsing (~170 LOC)
  app/pipeline/react/prompts.py      — prompts, modes, reasoning context (~320 LOC)

Pass 2 (future) will extract the dispatcher (_execute_tool + helpers,
~1,200 LOC) and the integrator. Keeping pass 1 small + surgical because
the dispatcher's internal cross-references are too dense to split safely
in one sitting.

This test does three things:

  1. Asserts the new modules exist and expose the expected identifiers.
  2. Asserts react_loop re-exports those identifiers so every existing
     caller (tests + any external import) keeps working.
  3. Ratchets react_loop.py LOC — set below the current size so any
     regrowth fails CI and forces an explicit bump. Same pattern main.py
     has; prevents the next silent monolith from forming.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO = Path(__file__).parent.parent
REACT_LOOP = REPO / "app" / "pipeline" / "react_loop.py"
REACT_PKG = REPO / "app" / "pipeline" / "react"


# ── New package layout exists ─────────────────────────────────────────────


class TestNewPackageLayout:
    def test_react_package_created(self):
        assert REACT_PKG.is_dir(), (
            "app/pipeline/react/ package missing — pass 1 of the split "
            "must land it or the remaining extractions have nowhere to go."
        )
        assert (REACT_PKG / "__init__.py").exists()

    def test_parsing_module_exists_with_expected_exports(self):
        mod = REACT_PKG / "parsing.py"
        assert mod.exists()
        from app.pipeline.react import parsing
        # Every function the old react_loop offered from this cluster
        # must be exposed here now.
        for name in (
            "_strip_markdown_json_fence",
            "_extract_balanced_json_object",
            "_parse_react_decision_dict_obj",
            "_parse_react_decision_json",
            "_react_fallback_org_npi_lookup_decision",
        ):
            assert hasattr(parsing, name), (
                f"{name} missing from app.pipeline.react.parsing — "
                f"the split dropped a function on the floor."
            )

    def test_prompts_module_exists_with_expected_exports(self):
        mod = REACT_PKG / "prompts.py"
        assert mod.exists()
        from app.pipeline.react import prompts
        for name in (
            "REACT_MAX_ROUNDS_COPILOT",
            "REACT_MAX_ROUNDS_AGENTIC",
            "REACT_MAX_ROUNDS_QUICK",
            "QUICK_MODE_TRUNCATED_CHARS",
            "react_chat_mode_label",
            "react_max_iterations_for_mode",
            "_react_round_headline",
            "_react_reasoning_system",
            "_get_config_sha",
            "_call_llm_json",
            "build_reasoning_context",
        ):
            assert hasattr(prompts, name), (
                f"{name} missing from app.pipeline.react.prompts."
            )


# ── Back-compat re-exports ────────────────────────────────────────────────


class TestBackCompatReExports:
    """External callers import from react_loop today. After pass 1 those
    names must still resolve at the old module path so we don't have to
    grep + rewrite every import site in one go. New code should use the
    new paths; old imports keep working.
    """

    def test_react_loop_reexports_parsing_names(self):
        from app.pipeline import react_loop
        # Every parser function must still be reachable via the old path.
        for name in (
            "_strip_markdown_json_fence",
            "_extract_balanced_json_object",
            "_parse_react_decision_dict_obj",
            "_parse_react_decision_json",
            "_react_fallback_org_npi_lookup_decision",
        ):
            assert hasattr(react_loop, name), (
                f"Back-compat re-export missing: react_loop.{name}. "
                f"Without it, old callers break."
            )

    def test_react_loop_reexports_prompts_names(self):
        from app.pipeline import react_loop
        for name in (
            "REACT_MAX_ROUNDS_COPILOT",
            "REACT_MAX_ROUNDS_AGENTIC",
            "REACT_MAX_ROUNDS_QUICK",
            "react_chat_mode_label",
            "react_max_iterations_for_mode",
            "_react_round_headline",
            "build_reasoning_context",
            "_call_llm_json",
        ):
            assert hasattr(react_loop, name), (
                f"Back-compat re-export missing: react_loop.{name}"
            )

    def test_reexported_values_actually_match(self):
        """Not just names — the re-exports must point to the SAME objects
        as the new modules, not new definitions that happen to share names."""
        from app.pipeline import react_loop
        from app.pipeline.react import parsing, prompts
        assert react_loop._parse_react_decision_json is parsing._parse_react_decision_json
        assert react_loop.build_reasoning_context is prompts.build_reasoning_context
        assert react_loop.REACT_MAX_ROUNDS_COPILOT == prompts.REACT_MAX_ROUNDS_COPILOT


# ── Behavioral sanity — the extracted funcs still work ───────────────────


class TestExtractedBehaviorUnchanged:
    """Quick behavioral smoke tests on the moved functions. Not exhaustive
    (the full planner/dispatcher test surface lives in test_react_*.py);
    just enough to catch "I moved it but it no longer imports its deps."
    """

    def test_strip_markdown_json_fence_still_strips(self):
        from app.pipeline.react.parsing import _strip_markdown_json_fence
        assert _strip_markdown_json_fence('```json\n{"a":1}\n```') == '{"a":1}'

    def test_parse_json_handles_clean_body(self):
        from app.pipeline.react.parsing import _parse_react_decision_json
        obj = _parse_react_decision_json('{"tool": "search_corpus", "is_complete": false}')
        assert obj is not None
        assert obj.get("tool") == "search_corpus"

    def test_parse_json_handles_markdown_fence(self):
        """LLM wrapping the body in ```json ... ``` must be stripped."""
        from app.pipeline.react.parsing import _parse_react_decision_json
        obj = _parse_react_decision_json('```json\n{"tool": "search_corpus"}\n```')
        assert obj is not None
        assert obj.get("tool") == "search_corpus"

    def test_parse_json_extracts_balanced_object_from_prose(self):
        """When the LLM spits prose + the JSON block inline, the balanced-
        object extractor pulls out just the JSON. This is the 'third tier'
        of the parser and the one most likely to silently break in
        extraction — the walk is stateful."""
        from app.pipeline.react.parsing import _parse_react_decision_json
        msg = 'Sure, here is my decision: {"tool": "google_search", "inputs": {"query": "x"}} — let me know.'
        obj = _parse_react_decision_json(msg)
        assert obj is not None
        assert obj.get("tool") == "google_search"

    def test_react_max_iterations_matches_mode(self):
        from app.pipeline.react.prompts import react_max_iterations_for_mode
        assert react_max_iterations_for_mode("copilot") == 3
        assert react_max_iterations_for_mode("agentic") == 10  # 2026-04-24: bumped 6→10
        assert react_max_iterations_for_mode("quick") == 2
        assert react_max_iterations_for_mode(None) == 3

    def test_round_headline_progression(self):
        from app.pipeline.react.prompts import _react_round_headline
        # Round 0 is scoping, round 1 is grounding. The last round used
        # to render "Finalize"; the 2026-04-19 guidance-mode commit
        # swapped it to "Guidance — ..." on rounds in the 80/20
        # synthesis band (which for copilot's 3-round mode is just the
        # last round). Explicit coverage of both names lives in
        # tests/test_react_guidance_mode.py; here we just assert the
        # last-round headline is NON-EMPTY and NOT still saying
        # "Finalize" (that would mean the guidance-mode label path
        # never wired up).
        assert "Scoping" in _react_round_headline(0, 3)
        assert "Grounding" in _react_round_headline(1, 3)
        last = _react_round_headline(2, 3)
        assert last, "last-round headline must not be empty"
        assert "Guidance" in last, (
            f"expected last-round headline to render 'Guidance' after "
            f"guidance-mode wiring; got {last!r}"
        )

    def test_fallback_decision_captures_org_name(self):
        """The NPI-lookup fallback is the one piece of parsing that
        reads ctx.message — smoke-test that the move didn't break the
        regex or the ctx plumbing."""
        from types import SimpleNamespace
        from app.pipeline.react.parsing import _react_fallback_org_npi_lookup_decision
        ctx = SimpleNamespace(
            effective_message="find NPIs for Sunshine Health",
            message="find NPIs for Sunshine Health",
        )
        decision = _react_fallback_org_npi_lookup_decision(ctx)
        assert decision is not None
        assert decision["tool"] == "lookup_npi"
        assert "sunshine" in decision["inputs"]["org_name"].lower()


# ── Ratchet: react_loop.py LOC cap ────────────────────────────────────────


class TestReactLoopRatchet:
    """The main.py hygiene guard catches monolith growth there; the 2026-
    04-18 audit showed react_loop.py had grown to 2,459 LOC unwatched.
    Adding the same pattern here: every future extraction should
    tighten this ceiling. Never loosen.

    Sub-pass log:
      pre-1i pass 1          2,459 LOC   (watched-file monolith risk discovered)
      post-1i pass 1         ~2,086 LOC  (parsing + prompts extracted)
      post cred-disconnect   ~1,405 LOC  (7 credentialing/roster tool
                                           branches + 5 helper functions
                                           removed 2026-04-18; planner
                                           manifest no longer advertises
                                           them so no dispatch reaches
                                           the removed code regardless)
      post-1i pass 2         TBD         (dispatcher extraction)
      post-1i pass 3         TBD         (integrator extraction)
    """

    MAX_REACT_LOOP_LOC = 2_260  # 2026-05-04: +55 LOC for _classify_query_strategy (auto-mode classifier: precision/recall/corpus) + wiring into search_corpus handler. Previously +41 LOC for thinking-chain emit signals.  # 2026-04-24: +130 LOC for retrieval taxonomy (Sprint 2 #0.2): _TOOL_ALIASES (~30 LOC), _normalize_tool_name (~10 LOC), precision_search dispatch (~80 LOC), comment header (~10 LOC). R2 refactor still tracked.   # 2026-04-23: +200 LOC absorbing three independent features landed this sprint — system_context Round 0 integration (~30 LOC), cache-assist seed_tool_results consumption (~15 LOC), critic-skip on pre-audited cache hits including the _cache_preaudited_critic_skip gate (~60 LOC + docs). R2 refactor (extract _execute_tool to react/dispatcher.py) is tracked on the production-readiness plan and will claw this back to ~1_500 when it lands.
    # 2026-04-18: bumped from 1_420 by 10 LOC to absorb the restore of
    # _attach_result_summary (renamed from the deleted
    # _attach_credentialing_result_summary). The utility is not
    # credentialing-specific — healthcare_query + healthcare_npi_lookup
    # both need it to summarize long NPPES payloads.
    # 2026-04-19: bumped from 1_430 to 1_510 (+80 LOC) for the ReAct
    # critic gate — an LLM-based groundedness check that runs when the
    # planner emits is_complete=true. The critic body is in
    # app/pipeline/react/critic.py (382 LOC, extracted); what sits in
    # the loop is just the call site, the round-control logic
    # (inject critique on reject, ship-with-warning on rounds-exhausted),
    # and the feature-flag gate. This is a real new feature, not drift —
    # the alternative was a post-hoc groundedness gate downstream of
    # the integrator, which doesn't get the retry benefit.
    # 2026-04-19 (later): bumped from 1_510 to 1_590 (+80 LOC) for
    # _corpus_confidence_min() — the tunable knob that replaces a
    # hardcoded confidence_min=0.5 at react_loop.py's search_corpus
    # call site. Live validation exposed the 0.5 threshold as silently
    # dropping abstain-grade chunks that were legitimately relevant;
    # moving to an env-var-tunable default-0.3 lets operators iterate
    # without redeploys. Helper body + explanation comment account for
    # the LOC; tests that lock the behavior are in
    # test_corpus_confidence_tuning.py.
    # 2026-04-19 (Sprint A.1 commit 1): bumped from 1_590 to 1_660
    # (+70 LOC) for the critic block's migration to structured emit
    # envelopes. The envelope types + helpers are in
    # app/communication/emit_envelope.py (separate module); what
    # lives here is the 5 make_critic_* import calls, the retry
    # counter, and the if/else branches that pick the right helper
    # for each critic outcome (audit_started, flagged, approved,
    # approved_after_retry, rounds_exhausted). Event-sourced
    # thinking_log is the foundation for Sprint A.2's task-manager
    # promotion — the LOC is justified by the analytics surface it
    # unblocks.
    # 2026-04-19 (Sprint A.1 commit 3): bumped from 1_660 to 1_700
    # (+40 LOC) for the fan-out of structured envelopes at the
    # tool_exhausted site (guard-block) and the guidance_mode_activated
    # site (transition round in the main loop). Envelope helpers
    # themselves live in app/communication/emit_envelope.py; what's
    # here is the wiring + conditional imports + the
    # _guidance_mode_emitted latch.

    def test_react_loop_loc_under_ceiling(self):
        loc = len(REACT_LOOP.read_text().splitlines())
        assert loc <= self.MAX_REACT_LOOP_LOC, (
            f"app/pipeline/react_loop.py is {loc} LOC, over the Phase 1i "
            f"ceiling ({self.MAX_REACT_LOOP_LOC}). Either continue the split "
            f"(pass 2 extracts _execute_tool to react/dispatcher.py), or "
            f"tighten the ceiling deliberately if something grew for a "
            f"good reason (don't bump it on autopilot)."
        )
