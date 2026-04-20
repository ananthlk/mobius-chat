"""Tests for tool agent (skills delegating to mobius-skills-core).

2026-04-20 (skills-core refactor): previously these tests mocked
``call_mcp_tool`` because the chat reached Google / the web-scraper
via the MCP server (chat → :8006 MCP → :8004/:8002 microservices).
Now the chat delegates straight to ``mobius_skills_core.skills.*``
which calls the microservices directly, so the mock point moved.
Behavior contracts (what answer the chat produces given what the
underlying skill returns) are unchanged and still asserted here.
"""
import pytest
from unittest.mock import patch

from mobius_skills_core import SkillResult, SourceRef
from app.services.doc_assembly import RETRIEVAL_SIGNAL_NO_SOURCES, RETRIEVAL_SIGNAL_GOOGLE_ONLY
from app.services.tool_agent import answer_tool, web_scrape_review_mcp_arguments


# ── Mock factories ───────────────────────────────────────────────────


def _google_ok_result(text: str, raw_results: list[dict] | None = None) -> SkillResult:
    """Build a SkillResult matching what run_google_search returns on success."""
    return SkillResult(
        text=text,
        sources=[SourceRef(document_name="example.com", source_type="web",
                           url="https://example.com", index=1)],
        signal="ok",
        extra={
            "results": raw_results or [
                {"title": "Title", "snippet": "Snippet",
                 "url": "https://example.com"},
            ],
            "query": "mocked",
        },
    )


def _google_no_sources() -> SkillResult:
    return SkillResult(text="No search results found.", signal="no_sources")


def _scrape_ok_result(content: str, url: str = "https://example.com/page",
                     mode: str = "quick") -> SkillResult:
    """SkillResult matching run_web_scrape's success shape (URL:… header + Content:)."""
    body = f"URL: {url}\n\nscrape_mode: {mode}\n\nContent:\n{content}"
    return SkillResult(
        text=body,
        sources=[SourceRef(document_name="example.com", source_type="web",
                           url=url, index=1)],
        signal="ok",
        extra={"mode": mode, "truncated": False, "summary": None},
    )


# ── google_search — the chat's legacy LLM-summary path (non-raw mode) ──


def test_tool_agent_google_search_calls_shared_core():
    """Search trigger + invoke_google_for_search_request → delegates to
    mobius_skills_core run_google_search."""
    with patch("mobius_skills_core.skills.google_search.run_google_search") as mock_search, \
         patch("app.services.tool_agent._run_google_search") as mock_wrapper:
        # _run_google_search wraps the shared fn and also handles LLM
        # summarization for the non-raw path. Mock the wrapper so we
        # stay focused on the chat's decision to call search at all;
        # the wrapper's internal delegation is covered below.
        mock_wrapper.return_value = (
            "Summarized answer citing [1]",
            [{"index": 1, "document_name": "Web search", "source_type": "external"}],
            None,
            RETRIEVAL_SIGNAL_GOOGLE_ONLY,
        )
        answer, sources, usage, signal = answer_tool(
            "Search for Florida Medicaid eligibility",
            invoke_google_for_search_request=True,
        )
        mock_wrapper.assert_called_once()
        # Verify the wrapper was invoked with the search query (first positional)
        assert mock_wrapper.call_args[0][0] == "Florida Medicaid eligibility"
        assert signal == RETRIEVAL_SIGNAL_GOOGLE_ONLY
        assert len(sources) > 0


def test_tool_agent_google_search_delegates_to_skills_core():
    """Lock the inner delegation — _run_google_search must call
    mobius_skills_core.run_google_search (and no longer call_mcp_tool)."""
    from app.services.tool_agent import _run_google_search

    with patch("mobius_skills_core.skills.google_search.run_google_search") as mock_core, \
         patch("app.services.tool_agent.asyncio.run") as mock_asyncio:
        mock_core.return_value = _google_ok_result(
            "1. Title — Snippet (https://example.com)"
        )
        mock_asyncio.return_value = ("Mocked LLM summary", None)

        answer, sources, usage, signal = _run_google_search(
            "Florida Medicaid eligibility"
        )
        mock_core.assert_called_once()
        kw = mock_core.call_args.kwargs
        assert kw["query"] == "Florida Medicaid eligibility"
        assert kw["max_results"] == 5
        assert signal == RETRIEVAL_SIGNAL_GOOGLE_ONLY


def test_tool_agent_google_search_no_results_returns_message():
    """Skill returns no_sources → wrapper emits friendly message."""
    from app.services.tool_agent import _run_google_search

    with patch("mobius_skills_core.skills.google_search.run_google_search") as mock_core:
        mock_core.return_value = _google_no_sources()
        answer, sources, usage, signal = _run_google_search("obscure xyz123")
        assert "No search results" in answer or "ran into an issue" in answer
        assert signal == RETRIEVAL_SIGNAL_NO_SOURCES


# ── web_scrape — chat's direct-URL path ──


def test_tool_agent_web_scrape_calls_shared_core():
    """Scrape trigger + URL → delegates to mobius_skills_core run_web_scrape."""
    with patch("app.stages.agents.capabilities.get_capability_answer", return_value=None), \
         patch("mobius_skills_core.skills.web_scrape.run_web_scrape") as mock_core:
        mock_core.return_value = _scrape_ok_result("Page content here...")
        answer, sources, usage, signal = answer_tool(
            "Scrape https://example.com/page",
        )
        mock_core.assert_called_once()
        kw = mock_core.call_args.kwargs
        assert kw["url"] == "https://example.com/page"
        assert kw["scrape_mode"] == "quick"
        assert kw["include_summary"] is False
        assert "Page content" in answer
        assert signal == RETRIEVAL_SIGNAL_GOOGLE_ONLY


def test_web_scrape_review_mcp_arguments_detailed():
    """The legacy arg-builder still returns the correct mode spec — the
    constants moved to mobius-skills-core but the chat re-exports via
    this helper for any caller still using it."""
    d = web_scrape_review_mcp_arguments("https://a.gov/x", scrape_mode="detailed")
    assert d["scrape_mode"] == "detailed"
    assert d["max_depth"] == 5 and d["max_pages"] == 50 and d["max_doc_downloads"] == 10


def test_tool_agent_web_scrape_detailed_passes_mode_to_skills_core():
    """Planner-picked scrape_mode=detailed must reach the shared skill
    as scrape_mode='detailed'. Mode spec forwarding is the shared
    skill's responsibility now — this test only verifies the chat
    passes through the mode string."""
    with patch("app.stages.agents.capabilities.get_capability_answer", return_value=None), \
         patch("mobius_skills_core.skills.web_scrape.run_web_scrape") as mock_core:
        mock_core.return_value = _scrape_ok_result("content", mode="detailed")
        answer_tool(
            "x",
            tool_hint_override="web_scrape",
            scrape_url="https://example.com/site",
            tool_inputs={"scrape_mode": "detailed"},
        )
        mock_core.assert_called_once()
        kw = mock_core.call_args.kwargs
        assert kw["scrape_mode"] == "detailed"


def test_tool_agent_web_scrape_no_url():
    """Scrape trigger without URL → helpful message, no skill call."""
    with patch("app.stages.agents.capabilities.get_capability_answer", return_value=None), \
         patch("mobius_skills_core.skills.web_scrape.run_web_scrape") as mock_core:
        answer, sources, usage, signal = answer_tool("Scrape this page for me")
        mock_core.assert_not_called()
        assert "I can scrape" in answer
        assert "URL" in answer


def test_tool_agent_handles_skill_error():
    """Shared skill returns tool_error → graceful fallback, no crash."""
    with patch("app.stages.agents.capabilities.get_capability_answer", return_value=None), \
         patch("mobius_skills_core.skills.web_scrape.run_web_scrape") as mock_core:
        mock_core.return_value = SkillResult(
            text="Scrape failed (network: connection refused).",
            signal="tool_error",
        )
        answer, sources, usage, signal = answer_tool(
            "Scrape https://example.com",
        )
        # Chat surfaces the skill's error text verbatim
        assert "Scrape failed" in answer or "ran into an issue" in answer
        assert sources == []
        assert signal == RETRIEVAL_SIGNAL_NO_SOURCES


def test_tool_agent_handles_null_result():
    """Skill returns no_sources with empty text → fallback message used."""
    with patch("app.stages.agents.capabilities.get_capability_answer", return_value=None), \
         patch("mobius_skills_core.skills.web_scrape.run_web_scrape") as mock_core:
        mock_core.return_value = SkillResult(text="", signal="no_sources")
        answer, sources, usage, signal = answer_tool(
            "Scrape https://example.com",
        )
        assert answer  # Should have fallback message
        assert isinstance(answer, str)


def test_tool_agent_capability_fallback_without_invoke():
    """Search trigger but invoke_google_for_search_request=False →
    capability message, no skill call."""
    with patch("mobius_skills_core.skills.google_search.run_google_search") as mock_core:
        answer, sources, usage, signal = answer_tool(
            "Search for Florida Medicaid",
            invoke_google_for_search_request=False,
        )
        mock_core.assert_not_called()
        assert "I can search" in answer or "search the web" in answer.lower()
