"""Tests for tool agent (MCP-based skills: google_search, web_scrape_review)."""
import pytest
from unittest.mock import patch

from app.services.doc_assembly import RETRIEVAL_SIGNAL_NO_SOURCES, RETRIEVAL_SIGNAL_GOOGLE_ONLY
from app.services.tool_agent import answer_tool, web_scrape_review_mcp_arguments


def test_tool_agent_google_search_calls_mcp():
    """Search trigger + invoke_google_for_search_request → calls MCP google_search."""
    with patch("app.services.tool_agent.call_mcp_tool") as mock_mcp:
        mock_mcp.return_value = ("[1] Title\n    Snippet\n    URL: https://example.com", True)
        answer, sources, usage, signal = answer_tool(
            "Search for Florida Medicaid eligibility",
            invoke_google_for_search_request=True,
        )
        mock_mcp.assert_called_once_with("google_search", {"query": "Florida Medicaid eligibility", "max_results": 5})
        assert signal == RETRIEVAL_SIGNAL_GOOGLE_ONLY
        assert len(sources) > 0


def test_tool_agent_google_search_no_results_returns_message():
    """Search returns 'No search results found' → returns message, no LLM summarization."""
    with patch("app.services.tool_agent.call_mcp_tool") as mock_mcp:
        mock_mcp.return_value = ("No search results found.", True)
        answer, sources, usage, signal = answer_tool(
            "Search for obscure xyz123 query",
            invoke_google_for_search_request=True,
        )
        assert "No search results" in answer or "ran into an issue" in answer
        assert signal == RETRIEVAL_SIGNAL_NO_SOURCES


def test_tool_agent_web_scrape_calls_mcp():
    """Scrape trigger + URL → calls MCP web_scrape_review, returns google_only signal."""
    with patch("app.stages.agents.capabilities.get_capability_answer", return_value=None):
        with patch("app.services.tool_agent.call_mcp_tool") as mock_mcp:
            mock_mcp.return_value = ("Page content here...", True)
            answer, sources, usage, signal = answer_tool(
                "Scrape https://example.com/page",
            )
            mock_mcp.assert_called_once()
            assert mock_mcp.call_args[0][0] == "web_scrape_review"
            args = mock_mcp.call_args[0][1]
            assert args["url"] == "https://example.com/page"
            assert args["include_summary"] is False
            assert args["scrape_mode"] == "quick"
            assert args["max_depth"] == 1 and args["max_pages"] == 1
            assert "Page content" in answer
            assert signal == RETRIEVAL_SIGNAL_GOOGLE_ONLY


def test_web_scrape_review_mcp_arguments_detailed():
    d = web_scrape_review_mcp_arguments("https://a.gov/x", scrape_mode="detailed")
    assert d["scrape_mode"] == "detailed"
    assert d["max_depth"] == 5 and d["max_pages"] == 50 and d["max_doc_downloads"] == 10


def test_tool_agent_web_scrape_detailed_passes_limits_and_timeout():
    with patch("app.stages.agents.capabilities.get_capability_answer", return_value=None):
        with patch("app.services.tool_agent.call_mcp_tool") as mock_mcp:
            mock_mcp.return_value = ("long content " * 50, True)
            answer_tool(
                "x",
                tool_hint_override="web_scrape",
                scrape_url="https://example.com/site",
                tool_inputs={"scrape_mode": "detailed"},
            )
            mock_mcp.assert_called_once()
            args = mock_mcp.call_args[0][1]
            assert args["scrape_mode"] == "detailed"
            assert args["max_pages"] == 50
            kw = mock_mcp.call_args[1]
            assert kw.get("read_timeout") == 300.0


def test_tool_agent_web_scrape_no_url():
    """Scrape trigger without URL → helpful message, no MCP call."""
    with patch("app.stages.agents.capabilities.get_capability_answer", return_value=None):
        with patch("app.services.tool_agent.call_mcp_tool") as mock_mcp:
            answer, sources, usage, signal = answer_tool("Scrape this page for me")
            mock_mcp.assert_not_called()
            assert "I can scrape" in answer
            assert "URL" in answer


def test_tool_agent_handles_mcp_exception():
    """call_mcp_tool raises → graceful error, no crash."""
    with patch("app.stages.agents.capabilities.get_capability_answer", return_value=None):
        with patch("app.services.tool_agent.call_mcp_tool") as mock_mcp:
            mock_mcp.side_effect = ConnectionError("MCP server unreachable")
            answer, sources, usage, signal = answer_tool(
                "Scrape https://example.com",
            )
            assert "ran into an issue" in answer or "Please try again" in answer
            assert sources == []
            assert signal == RETRIEVAL_SIGNAL_NO_SOURCES


def test_tool_agent_handles_null_result():
    """call_mcp_tool returns (None, True) → no crash, empty string used."""
    with patch("app.stages.agents.capabilities.get_capability_answer", return_value=None):
        with patch("app.services.tool_agent.call_mcp_tool") as mock_mcp:
            mock_mcp.return_value = (None, True)
            answer, sources, usage, signal = answer_tool(
                "Scrape https://example.com",
            )
            assert answer  # Should have fallback message
            assert isinstance(answer, str)


def test_tool_agent_capability_fallback_without_invoke():
    """Search trigger but invoke_google_for_search_request=False → capability message."""
    with patch("app.services.tool_agent.call_mcp_tool") as mock_mcp:
        answer, sources, usage, signal = answer_tool(
            "Search for Florida Medicaid",
            invoke_google_for_search_request=False,
        )
        mock_mcp.assert_not_called()
        assert "I can search" in answer or "search the web" in answer.lower()
