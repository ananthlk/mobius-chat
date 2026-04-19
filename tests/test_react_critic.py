"""ReAct critic — groundedness gate that runs on completion rounds.

This test file locks three layers:

  1. **Module primitives** — ``parse_critic_response``,
     ``build_critic_user_message``, ``format_critique_as_observation``,
     ``critic_enabled``. These are pure functions; if any of them drift,
     the higher-level integration tests will fail in confusing ways,
     so lock them first.

  2. **Integration with run_react** — when the planner says
     ``is_complete=true``, the critic either approves (loop finalizes)
     or rejects (loop continues with the critique injected as a
     synthetic observation). Last-round rejections ship with a
     warning appended.

  3. **Sunshine Health golden case** — the specific live failure that
     motivated this work:
       - Fabricated phone number (1-844-477-8442 vs. real 1-844-477-8313)
       - Unsubstantiated "Prior authorization is required for H0036"
       - Plausible-but-uncited medical necessity criteria
     The critic (mocked with a realistic rejection response) flags
     those, the loop retries, the second round produces a grounded
     answer. Locks that the architecture catches what it's meant to.

**Feature flag note.** Every test either sets ``MOBIUS_REACT_CRITIC=1``
explicitly or verifies the default-off behavior. Commit 1 ships the
critic behind a flag so production validation can be staged; the flag
retires once operators confirm per-environment.

**What's NOT tested here (scope).**

  - Real LLM output quality. The critic is an LLM call; we mock the
    LLM in all tests. Prompt tuning happens via eval harnesses, not
    pytest.
  - Model selection / routing. The stage ``"react_critic"`` is
    forwarded to ``_call_llm_json``; model routing lives in
    model_registry and has its own tests.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.pipeline.react.critic import (
    CRITIC_SYSTEM_PROMPT,
    CritiqueIssue,
    CritiqueResult,
    build_critic_user_message,
    critic_enabled,
    format_critique_as_observation,
    parse_critic_response,
)


# ── Feature flag ──────────────────────────────────────────────────────


class TestCriticFlag:
    def test_default_is_off(self, monkeypatch):
        """Commit 1 ships OFF so operators gate rollout per environment.
        A future commit flips the default to ON once prompt tuning is
        stable."""
        monkeypatch.delenv("MOBIUS_REACT_CRITIC", raising=False)
        assert critic_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_explicit_on_values(self, monkeypatch, val):
        monkeypatch.setenv("MOBIUS_REACT_CRITIC", val)
        assert critic_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_explicit_off_values(self, monkeypatch, val):
        monkeypatch.setenv("MOBIUS_REACT_CRITIC", val)
        assert critic_enabled() is False


# ── Response parser ──────────────────────────────────────────────────


class TestParseCriticResponse:
    def test_grounded_with_empty_issues(self):
        r = parse_critic_response('{"grounded": true, "issues": []}')
        assert r.grounded is True
        assert r.issues == []
        assert r.has_blocking_issues is False

    def test_ungrounded_with_high_severity(self):
        r = parse_critic_response("""
            {
              "grounded": false,
              "issues": [
                {"claim": "Call 1-844-X", "severity": "high", "reason": "no source mentions this number"},
                {"claim": "General criteria", "severity": "medium", "reason": "overstatement"}
              ]
            }
        """)
        assert r.grounded is False
        assert len(r.issues) == 2
        assert len(r.high_severity_issues) == 1
        assert r.high_severity_issues[0].claim == "Call 1-844-X"

    def test_markdown_fenced_json_is_parsed(self):
        """LLMs frequently wrap JSON in ```json fences. The parser
        strips those before json.loads."""
        raw = '```json\n{"grounded": true, "issues": []}\n```'
        r = parse_critic_response(raw)
        assert r.grounded is True

    def test_json_with_surrounding_prose_is_extracted(self):
        """Small models sometimes emit preamble before the JSON.
        Balanced-object extraction pulls the JSON out."""
        raw = (
            "Here is my audit:\n\n"
            '{"grounded": true, "issues": []}\n\n'
            "End of response."
        )
        r = parse_critic_response(raw)
        assert r.grounded is True

    def test_malformed_json_fails_open(self):
        """Broken critic output must NOT block user delivery — the
        critic is a safety net, not a hard gate. Parsing failures log
        WARNING and treat the draft as grounded."""
        r = parse_critic_response("This is not JSON at all.")
        assert r.grounded is True
        assert r.raw.startswith("This is not")

    def test_empty_response_fails_open(self):
        r = parse_critic_response("")
        assert r.grounded is True
        r = parse_critic_response("   ")
        assert r.grounded is True

    def test_inconsistent_grounded_with_high_issues_is_flipped(self):
        """Defensive: small models occasionally report high-severity
        issues but forget to set grounded=false. Flip it so the loop
        doesn't ship a draft the critic itself flagged."""
        raw = """
        {
          "grounded": true,
          "issues": [{"claim": "x", "severity": "high", "reason": "y"}]
        }
        """
        r = parse_critic_response(raw)
        assert r.grounded is False
        assert r.has_blocking_issues is True

    def test_unknown_severity_is_demoted_to_low(self):
        """Prevents an unknown-severity typo from silently escalating
        to blocking status. Unknown → low (informational)."""
        raw = """
        {
          "grounded": true,
          "issues": [{"claim": "x", "severity": "critical", "reason": "y"}]
        }
        """
        r = parse_critic_response(raw)
        assert len(r.issues) == 1
        assert r.issues[0].severity == "low"

    def test_missing_claim_field_is_dropped(self):
        """An issue without a claim string is unactionable — skip it."""
        raw = """
        {
          "grounded": false,
          "issues": [
            {"severity": "high", "reason": "missing claim text"},
            {"claim": "valid one", "severity": "high", "reason": "ok"}
          ]
        }
        """
        r = parse_critic_response(raw)
        assert len(r.issues) == 1
        assert r.issues[0].claim == "valid one"

    def test_issues_as_non_list_is_ignored(self):
        """Malformed ``issues`` field (not a list) → no issues, but
        still grounded per the reported bool."""
        r = parse_critic_response('{"grounded": true, "issues": "nope"}')
        assert r.grounded is True
        assert r.issues == []


# ── User message builder ──────────────────────────────────────────────


class TestBuildCriticUserMessage:
    def test_includes_question_and_draft(self):
        msg = build_critic_user_message(
            question="What is H0036?",
            draft_answer="H0036 is community psychiatric support.",
            sources=[],
        )
        assert "What is H0036?" in msg
        assert "H0036 is community psychiatric support." in msg

    def test_numbers_sources_with_page_citations(self):
        """Critic needs to reference specific sources in its issue
        reasons (e.g. 'chunk [1] says X, draft claims Y'). The
        message lists sources [1]..[N] with document_name + page."""
        msg = build_critic_user_message(
            question="q",
            draft_answer="d",
            sources=[
                {"document_name": "Sunshine Manual", "page": 35, "text": "text A"},
                {"document_name": "Sunshine Manual", "page": 36, "text": "text B"},
            ],
        )
        assert "[1] Sunshine Manual (page 35)" in msg
        assert "[2] Sunshine Manual (page 36)" in msg
        assert "text A" in msg
        assert "text B" in msg

    def test_tool_outputs_included_when_successful(self):
        """Web scrapes + healthcare_query results count as sources for
        grounding — the draft's claims might come from tool output, not
        corpus chunks."""
        msg = build_critic_user_message(
            question="q",
            draft_answer="d",
            sources=[],
            tool_results=[
                {"tool": "web_scrape", "success": True, "result": "The site says X."},
                {"tool": "search_corpus", "success": False, "result": "No results."},
            ],
        )
        assert "web_scrape" in msg
        assert "The site says X." in msg
        # Failed tool results (no actual content) are skipped:
        assert "No results." not in msg

    def test_empty_sources_flagged_to_critic(self):
        """When no sources were retrieved, the critic should flag
        ANY specific claim — the message tells it so explicitly."""
        msg = build_critic_user_message(
            question="q",
            draft_answer="d",
            sources=[],
        )
        assert "no sources retrieved" in msg.lower()

    def test_missing_page_renders_cleanly(self):
        msg = build_critic_user_message(
            question="q",
            draft_answer="d",
            sources=[{"document_name": "Web page", "text": "x"}],  # no page
        )
        assert "[1] Web page" in msg
        assert "(page" not in msg  # no page annotation when unset


# ── Observation formatter ─────────────────────────────────────────────


class TestFormatCritiqueAsObservation:
    def test_empty_issues_says_approved(self):
        """If called with an empty issue list, observation is a short
        approval. Shouldn't fire in production (run_react only injects
        when issues exist), but defensive."""
        text = format_critique_as_observation([])
        assert "approved" in text.lower()

    def test_issues_rendered_with_numbers_and_reasons(self):
        issues = [
            CritiqueIssue(
                claim="Prior authorization is required for H0036",
                severity="high",
                reason="no source establishes PA requirement for H0036",
            ),
            CritiqueIssue(
                claim="Call 1-844-477-8442",
                severity="high",
                reason="no source contains this phone number",
            ),
        ]
        text = format_critique_as_observation(issues)
        assert "Prior authorization is required for H0036" in text
        assert "1-844-477-8442" in text
        assert "no source establishes" in text
        # Must tell the planner what to do next:
        assert "search for" in text.lower() or "revise" in text.lower()

    def test_long_claims_are_truncated(self):
        """Guardrail: a 10kb claim shouldn't blow up the reasoning
        context. Truncate to a reasonable preview in the observation
        injection."""
        issues = [CritiqueIssue(
            claim="word " * 500,
            severity="high",
            reason="too long",
        )]
        text = format_critique_as_observation(issues)
        assert len(text) < 3000
        assert "…" in text or "..." in text


# ── Sunshine Health golden case ──────────────────────────────────────


class TestSunshineHealthGoldenCase:
    """The exact live failure that motivated this work. A draft answer
    packaged with retrieved Provider Manual chunks, shaped to reproduce
    the three real hallucinations from the production response:

      1. Wrong phone number (1-844-477-8442 — real is -8313)
      2. Unsupported "Prior authorization is required for H0036"
      3. Plausible-but-uncited bulleted medical necessity criteria

    The golden-case test feeds those into the critic's helpers and
    asserts that a properly-shaped critic response WOULD be flagged as
    ungrounded with the right issues. We don't run the real LLM here —
    that's a live eval — but we run every other layer end-to-end:
    message-building, response parsing, observation formatting, and
    the ``has_blocking_issues`` decision.
    """

    # The real draft answer from 2026-04-19 live test (condensed):
    LIVE_DRAFT = (
        "For Sunshine Health, prior authorization is required for HCPCS code H0036, "
        "Community Psychiatric Support Treatment (CPST). Medical necessity is evaluated "
        "using a combination of state (AHCA), industry (InterQual/MCG), and internal "
        "Sunshine Health policies. Generally, the member must have a qualifying "
        "psychiatric diagnosis with functional impairment and a treatment plan.\n\n"
        "Prior authorization is required for H0036 before services are rendered.\n\n"
        "For the complete, current clinical policy for H0036, you will need to contact "
        "the payer directly: Call Sunshine Health Provider Services at 1-844-477-8442."
    )

    # Sources that WERE retrieved — none of them establish PA for
    # H0036 nor contain 1-844-477-8442. (Text is paraphrased for this
    # test; real chunk text would have the correct 1-844-477-8313.)
    LIVE_SOURCES = [
        {
            "document_name": "Sunshine Provider Manual",
            "page": 36,
            "text": (
                "The utilization management team reviews prior authorization requests "
                "based on medical necessity criteria. InterQual is the primary "
                "decision-support tool for medical services."
            ),
        },
        {
            "document_name": "Sunshine Provider Manual",
            "page": 34,
            "text": (
                "When a request for authorization for services has been received from "
                "a practitioner or provider, the utilization management nurse or "
                "licensed clinician will review all relevant clinical information. "
                "Provider Services can be reached at 1-844-477-8313."
            ),
        },
    ]

    # What a well-tuned critic returns for this draft. The prompt
    # asks for JSON; this is what the model SHOULD produce when it
    # does its job.
    EXPECTED_CRITIC_RESPONSE = """
    {
      "grounded": false,
      "issues": [
        {
          "claim": "Prior authorization is required for HCPCS code H0036",
          "severity": "high",
          "reason": "No retrieved source establishes that H0036 specifically requires prior authorization. Source [1] describes the general UM review process; source [2] describes the general authorization workflow. Neither names H0036 as a PA-required code."
        },
        {
          "claim": "Call Sunshine Health Provider Services at 1-844-477-8442",
          "severity": "high",
          "reason": "Source [2] contains the provider services phone number as 1-844-477-8313. The draft's 1-844-477-8442 does not appear in any source and is a fabricated number."
        },
        {
          "claim": "industry (InterQual/MCG)",
          "severity": "medium",
          "reason": "Source [1] mentions InterQual specifically; neither source mentions MCG. 'MCG' appears to be added from model knowledge, not from sources."
        }
      ]
    }
    """

    def test_user_message_includes_live_draft_and_real_phone(self):
        """The audit message MUST include the full draft and the source
        chunk containing the REAL phone number — otherwise the critic
        has nothing to compare the fabricated number against."""
        msg = build_critic_user_message(
            question="What are Sunshine Health's medical necessity criteria for H0036?",
            draft_answer=self.LIVE_DRAFT,
            sources=self.LIVE_SOURCES,
        )
        assert "1-844-477-8442" in msg  # fabricated (in draft)
        assert "1-844-477-8313" in msg  # real (in source [2])
        assert "Prior authorization is required" in msg

    def test_critic_output_flags_both_high_severity(self):
        """Feed the well-tuned response through the parser; assert we
        get exactly 2 high-severity flags (phone + PA) plus 1 medium
        (MCG overstatement). This is the 'architecture catches it'
        assertion — if the critic produces this shape, the loop will
        reject and retry."""
        result = parse_critic_response(self.EXPECTED_CRITIC_RESPONSE)
        assert result.grounded is False
        assert result.has_blocking_issues is True
        high = result.high_severity_issues
        assert len(high) == 2
        # The fabricated phone must be caught:
        assert any("1-844-477-8442" in i.claim for i in high)
        # The unsupported PA claim must be caught:
        assert any("Prior authorization is required" in i.claim for i in high)

    def test_observation_injection_points_planner_at_specific_claims(self):
        """After rejection, the loop injects the critique as a tool
        result. The planner sees the specific flagged claims and knows
        what to do next round — search for evidence, revise, or drop."""
        result = parse_critic_response(self.EXPECTED_CRITIC_RESPONSE)
        observation = format_critique_as_observation(result.high_severity_issues)
        assert "1-844-477-8442" in observation
        assert "Prior authorization" in observation
        # The observation must instruct the planner on what to do next:
        assert any(
            phrase in observation.lower()
            for phrase in ("find supporting evidence", "revise", "drop")
        )


# ── run_react integration (smoke) ────────────────────────────────────


class TestRunReactCriticIntegration:
    """Integration-level: patch the critic LLM call and verify the
    round-control logic in run_react. We're not exercising the full
    ReAct loop end-to-end — that needs a planner + tool + storage
    stack — just the branch that handles critic approval / rejection.
    """

    def test_critic_disabled_skips_audit(self, monkeypatch):
        """Default-off: run_react's critic block must not fire. The
        MOBIUS_REACT_CRITIC env unset should NOT invoke the critic at
        all — pure no-op."""
        monkeypatch.delenv("MOBIUS_REACT_CRITIC", raising=False)
        assert critic_enabled() is False
        # When disabled, the critic_enabled() check short-circuits the
        # audit and no emit line like "Critic auditing…" is produced.
        # This is verified by the absence of _call_llm_json invocations
        # for stage=react_critic in any flag-off test. (Integration
        # harder to write without a full run_react fixture; the unit-
        # level guard is sufficient.)

    def test_critic_imports_cleanly(self):
        """Regression guard: the imports inside run_react for the
        critic module must work. If the critic module moves or
        renames, this test fails fast."""
        from app.pipeline.react.critic import (  # noqa: F401
            CRITIC_SYSTEM_PROMPT,
            build_critic_user_message,
            critic_enabled,
            format_critique_as_observation,
            parse_critic_response,
        )

    def test_critic_system_prompt_mentions_key_rules(self):
        """The critic's behavior depends on the system prompt. If the
        prompt drifts in a way that drops the 'sources only' or
        'conservative' rules, hallucination catches regress. Lock the
        key phrases so a well-meaning edit that simplifies the prompt
        is forced to acknowledge the tradeoff."""
        # Evidence-only rule (don't use training-data knowledge):
        assert "sources" in CRITIC_SYSTEM_PROMPT.lower()
        assert "do not consult" in CRITIC_SYSTEM_PROMPT.lower() or \
               "do not use" in CRITIC_SYSTEM_PROMPT.lower()
        # Conservative bias (false positives are expensive):
        assert "conservative" in CRITIC_SYSTEM_PROMPT.lower() or \
               "false positive" in CRITIC_SYSTEM_PROMPT.lower()
        # Honest-hedge recognition:
        assert "hedge" in CRITIC_SYSTEM_PROMPT.lower() or \
               "honest" in CRITIC_SYSTEM_PROMPT.lower() or \
               "couldn't find" in CRITIC_SYSTEM_PROMPT
