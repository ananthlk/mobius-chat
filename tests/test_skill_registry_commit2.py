"""Skill registry commit 2: healthcare_query + web_scrape migrated.

What this file guards (in order of load-bearing importance):

1. **End-to-end parity.** For each migrated skill, register-path and
   legacy-path must produce identical ``answer_tool`` return tuples
   given the same mocked MCP response. If this ever diverges, commit 3
   cannot delete the legacy branch yet — the parity failure is the
   safety signal that says "fix the handler before retiring the
   fallback."

2. **URL fall-through.** The legacy ``hint == "web_scrape"`` branch
   rewrote ``hint = "google_search"`` when no URL was extractable. That
   behavior moved from the legacy cascade into the dispatcher shim
   above the registry lookup. Test that hint=web_scrape with no URL
   still reaches google_search — both paths.

3. **Envelope shape.** The ``SkillEnvelope`` returned by each migrated
   handler carries the exact fields the legacy 4-tuple produced: a
   single ``SourceRef`` for healthcare_query / web_scrape success,
   empty sources list on failure, correct ``signal`` value.

4. **Entity-extraction isolation.** healthcare_query must NOT receive
   active payer / jurisdiction as a search qualifier — the legacy
   branch was careful about this (the 2025 Tool Isolation spec). The
   migration preserves that via the same
   ``extract_entity_from_question`` call.

5. **Registry enumeration.** All 4 migrated skills registered; the
   drift-detection guard from commit 1 is updated.

Not tested here (scope belongs to commit 3):
  - ``google_search`` migration + auto-scrape preservation.
  - ``TOOL_MANIFEST`` auto-generation from the registry.
  - Deletion of the legacy ``if hint == "X"`` branches.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.skills import registry
from app.skills.registry import SkillCall


# ── Helpers ──────────────────────────────────────────────────────────


def _mcp_mock(text: str, success: bool):
    """Return a side_effect for call_mcp_tool that ignores args and
    returns a fixed (text, success) tuple. Matches the MCP manager's
    actual return shape so both registry and legacy paths see the same
    response."""

    def impl(tool, args, **kwargs):
        return (text, success)

    return impl


def _dual_mcp_patch(text: str, success: bool):
    """tool_agent.py's call_mcp_tool is bound at import time; skill
    builtins import ``call_mcp_tool`` via app.services.mcp_manager.
    Both have to be patched for parity tests to exercise a mocked MCP
    on both paths."""
    side = _mcp_mock(text, success)
    return (
        patch("app.services.tool_agent.call_mcp_tool", side_effect=side),
        patch("app.services.mcp_manager.call_mcp_tool", side_effect=side),
    )


# ── Registration drift-detection ─────────────────────────────────────


class TestExpectedSkillsRegistered:
    def test_commit2_skills_all_registered(self):
        """Commit 2 adds healthcare_query and web_scrape to the 2 already
        registered in commit 1. When commit 3 adds google_search, this
        expected list grows by one — failing this test forces the author
        to acknowledge the addition."""
        names = registry.all_names()
        expected = {
            "document_upload_skill",
            "list_thread_document_uploads",
            "healthcare_query",
            "web_scrape",
        }
        # Subset check (not equality) so a future commit that adds
        # google_search doesn't break this test from the past.
        assert expected <= names

    def test_healthcare_query_is_entity_tool(self):
        """healthcare_query never takes jurisdiction. Legacy
        ENTITY_TOOLS in tool_manifest.py has it; the derived view must
        too. Commit 3 will swap the hand-maintained set for this
        derived one — locking the contract early prevents surprises."""
        assert "healthcare_query" in registry.entity_tools()

    def test_web_scrape_is_entity_tool(self):
        assert "web_scrape" in registry.entity_tools()

    def test_neither_is_follow_up_capable(self):
        """Neither healthcare_query nor web_scrape survives as a
        follow-up context in the planner today (FOLLOW_UP_CAPABLE only
        contains list_thread_document_uploads post-disconnect). Locking."""
        fuc = registry.follow_up_capable()
        assert "healthcare_query" not in fuc
        assert "web_scrape" not in fuc


# ── Direct handler behavior ──────────────────────────────────────────


class TestHealthcareQueryHandler:
    def test_success_returns_envelope_with_source(self):
        """Happy path: MCP returns a non-error body → envelope has the
        text, a single SourceRef with document_name='Healthcare lookup',
        and signal='no_sources' (correct — not corpus, not google)."""
        cx = patch("app.services.mcp_manager.call_mcp_tool", side_effect=_mcp_mock(
            "NPI 1234567890: Jane Doe, Taxonomy 2084P0800X", True,
        ))
        with cx:
            env = registry.dispatch(
                SkillCall(
                    name="healthcare_query",
                    inputs={},
                    question="Look up NPI 1234567890",
                    user_message="Look up NPI 1234567890",
                )
            )
        assert "1234567890" in env.text
        assert env.signal == "no_sources"
        assert len(env.sources) == 1
        assert env.sources[0].document_name == "Healthcare lookup"
        assert env.sources[0].source_type == "external"
        # Preview is first 300 chars of the response.
        assert env.sources[0].text.startswith("NPI 1234567890")

    def test_mcp_error_response_returns_no_sources(self):
        """When MCP returns ``Error: ...``, handler must not wrap it in
        a SourceRef (legacy branch doesn't either). Signal stays
        no_sources."""
        cx = patch("app.services.mcp_manager.call_mcp_tool", side_effect=_mcp_mock(
            "Error: NPI not found", True,
        ))
        with cx:
            env = registry.dispatch(
                SkillCall(name="healthcare_query", inputs={}, question="Look up NPI 0000000000"),
            )
        assert env.sources == []
        assert env.signal == "no_sources"

    def test_mcp_exception_is_caught_with_helpful_message(self):
        """MCP crashed (healthcare API down) → handler returns a
        graceful error envelope, not a bare traceback. Matches legacy
        'I ran into an issue. {e}. Please try again.' shape."""
        def boom(tool, args, **kw):
            raise RuntimeError("healthcare API down")

        with patch("app.services.mcp_manager.call_mcp_tool", side_effect=boom):
            env = registry.dispatch(
                SkillCall(name="healthcare_query", inputs={}, question="Look up NPI 1"),
            )
        assert "ran into an issue" in env.text.lower()
        assert "healthcare api down" in env.text.lower()
        assert env.signal == "no_sources"

    def test_explicit_question_override_via_inputs(self):
        """Planner may pass a more precise ``question`` in tool_inputs;
        when set, the handler prefers it over entity-extracted text.
        Asserts the MCP call receives the override, not the raw
        question body."""
        captured = {}

        def cap(tool, args, **kw):
            captured["args"] = args
            return ("ok", True)

        with patch("app.services.mcp_manager.call_mcp_tool", side_effect=cap):
            registry.dispatch(
                SkillCall(
                    name="healthcare_query",
                    inputs={"question": "EXPLICIT QUESTION"},
                    question="raw text would be a fallback",
                )
            )
        assert captured["args"]["question"] == "EXPLICIT QUESTION"


class TestWebScrapeHandler:
    def test_url_from_inputs_triggers_scrape(self):
        """Dispatcher populates ``inputs['url']`` before calling the
        handler. Handler trusts it and calls the underlying MCP."""
        cx = patch("app.services.tool_agent.call_mcp_tool", side_effect=_mcp_mock(
            "# Policy Page\n\nEligibility details here.", True,
        ))
        with cx:
            env = registry.dispatch(
                SkillCall(
                    name="web_scrape",
                    inputs={"url": "https://example.com/policy"},
                    question="scrape this",
                )
            )
        assert "Policy Page" in env.text
        assert env.signal == "google_only"
        assert len(env.sources) == 1
        assert env.sources[0].url == "https://example.com/policy"
        assert env.sources[0].source_type == "web"

    def test_missing_url_returns_helpful_message(self):
        """Belt-and-suspenders: dispatcher should rewrite hint when no
        URL is available, but if the handler does get called with no
        URL (programmatic caller), it should produce the same 'need URL'
        text the keyword-path legacy branch does."""
        env = registry.dispatch(
            SkillCall(name="web_scrape", inputs={}, question="scrape something"),
        )
        assert "URL" in env.text
        assert env.signal == "no_sources"

    def test_scrape_mode_forwarded_to_mcp_arguments(self):
        """Planner can request medium or detailed mode. The handler
        must pass it through to the MCP call via
        ``web_scrape_review_mcp_arguments(scrape_mode=...)``."""
        captured = {}

        def cap(tool, args, **kw):
            captured["args"] = args
            return ("content", True)

        with patch("app.services.tool_agent.call_mcp_tool", side_effect=cap):
            registry.dispatch(
                SkillCall(
                    name="web_scrape",
                    inputs={"url": "https://x.com", "scrape_mode": "detailed"},
                    question="",
                )
            )
        # web_scrape_review_mcp_arguments packs mode into args — exact key
        # name depends on the helper. Assert the mode made it in somewhere.
        flat = str(captured["args"])
        assert "detailed" in flat


# ── End-to-end parity: registry vs. legacy branch ────────────────────


class TestRegistryParity:
    """The migration's safety net. Same input, same mocked MCP, both
    dispatch paths — outputs must be byte-identical. If this fails in
    CI after commit 2, it means a subtle divergence crept in and the
    commit shouldn't merge."""

    def _run_both_paths(self, mcp_text: str, mcp_ok: bool, question: str, hint: str):
        from app.services.tool_agent import answer_tool

        tool_patch, mgr_patch = _dual_mcp_patch(mcp_text, mcp_ok)
        with patch.dict(os.environ, {"MOBIUS_USE_SKILL_REGISTRY": "1"}):
            with tool_patch, mgr_patch:
                registry_result = answer_tool(question, tool_hint_override=hint)
        tool_patch, mgr_patch = _dual_mcp_patch(mcp_text, mcp_ok)
        with patch.dict(os.environ, {"MOBIUS_USE_SKILL_REGISTRY": "0"}):
            with tool_patch, mgr_patch:
                legacy_result = answer_tool(question, tool_hint_override=hint)
        return registry_result, legacy_result

    def test_healthcare_query_success_parity(self):
        r, l = self._run_both_paths(
            "NPI 1234567890: Jane Doe, Taxonomy 2084P0800X",
            True,
            "Look up NPI 1234567890",
            "healthcare_query",
        )
        assert r == l, (
            "healthcare_query registry and legacy paths diverged. "
            "Commit 3 must not delete the legacy branch until parity is restored."
        )

    def test_web_scrape_with_url_parity(self):
        r, l = self._run_both_paths(
            "# Policy\n\nEligibility info.",
            True,
            "scrape https://example.com/policy",
            "web_scrape",
        )
        assert r == l

    def test_web_scrape_no_url_falls_through_to_google_parity(self):
        """hint=web_scrape with no URL → both paths must rewrite the
        hint and end up calling the google_search path identically.
        (google_search itself is still on the legacy branch in commit 2,
        so both paths hit the same code; this test will still pass
        after commit 3 migrates google_search — the behavior is what
        we're asserting.)"""
        r, l = self._run_both_paths(
            "No search results found",  # legacy MCP returns this when empty
            False,
            "explain credentialing (no URL)",
            "web_scrape",
        )
        assert r == l
