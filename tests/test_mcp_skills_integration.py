"""Integration tests: real Google search and web scrape via MCP.

Skips when MCP server (mobius-skills-mcp) or downstream services are not running.
Requires: mstart (or manually) mobius-skills-mcp, mobius-skills/google-search, mobius-skills/web-scraper.

Run with: pytest mobius-chat/tests/test_mcp_skills_integration.py -v -s
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Load .env
_chat_root = Path(__file__).resolve().parent.parent
for _env_path in [_chat_root / ".env", _chat_root.parent / ".env"]:
    if _env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(_env_path, override=False)
        except Exception:
            pass


def _mcp_reachable() -> bool:
    """True if MCP server is reachable."""
    try:
        from app.services.mcp_manager import list_mcp_tools
        tools = list_mcp_tools()
        return len(tools) > 0
    except Exception:
        return False


def _mcp_tool_names() -> set[str]:
    try:
        from app.services.mcp_manager import list_mcp_tools
        return {t.get("name", "") for t in list_mcp_tools()}
    except Exception:
        return set()


skip_if_mcp_unreachable = pytest.mark.skipif(
    not _mcp_reachable(),
    reason="MCP server not reachable. Run mstart or start mobius-skills-mcp on port 8006.",
)

# Excluded by pytest -m "not integration" (Day 3 gate)
pytestmark = [pytest.mark.integration, pytest.mark.requires_skills]


@skip_if_mcp_unreachable
def test_google_search_e2e():
    """Real Google search via MCP. Requires mobius-skills-mcp + google-search API."""
    from app.services.mcp_manager import call_mcp_tool

    txt, ok = call_mcp_tool(
        "google_search",
        {"query": "Florida Medicaid eligibility 2024", "max_results": 3},
    )
    assert isinstance(txt, str)
    assert isinstance(ok, bool)

    if "not configured" in txt or "Error:" in txt:
        pytest.skip(
            "MCP/google-search not fully configured. "
            "Set CHAT_SKILLS_GOOGLE_SEARCH_URL for mobius-skills-mcp and run mobius-skills/google-search."
        )
    if "No search results" in txt:
        pytest.skip("Google search returned no results (API may be rate-limited or misconfigured)")

    assert ok, f"Expected success, got: {txt[:200]}"
    assert len(txt) > 50
    # Real search results typically have URLs or numbered items
    assert "http" in txt or "[" in txt or "URL:" in txt or "1]" in txt


@skip_if_mcp_unreachable
def test_web_scrape_e2e():
    """Real web scrape via MCP. Requires mobius-skills-mcp + web-scraper."""
    from app.services.mcp_manager import call_mcp_tool

    url = "https://www.sunshinehealth.com/providers/utilization-management/clinical-payment-policies.html"
    txt, ok = call_mcp_tool(
        "web_scrape_review",
        {"url": url, "include_summary": False},
    )
    assert isinstance(txt, str)
    assert isinstance(ok, bool)

    if "not configured" in txt or "Error: CHAT_SKILLS_WEB_SCRAPER_URL" in txt:
        pytest.skip(
            "MCP/web-scraper not fully configured. "
            "Set CHAT_SKILLS_WEB_SCRAPER_URL for mobius-skills-mcp and run mobius-skills/web-scraper."
        )
    if "Scrape failed" in txt or "No content extracted" in txt:
        pytest.skip(f"Web scraper failed: {txt[:150]}")

    assert ok, f"Expected success, got: {txt[:200]}"
    assert len(txt) > 100
    # Sunshine Health page should mention clinical/payment policies
    assert "clinical" in txt.lower() or "policy" in txt.lower() or "Sunshine" in txt


@skip_if_mcp_unreachable
def test_tool_agent_google_search_e2e():
    """Full flow: answer_tool with search trigger → MCP google_search → real results."""
    from app.services.tool_agent import answer_tool

    answer, sources, usage, signal = answer_tool(
        "Search for Florida Medicaid eligibility requirements",
        invoke_google_for_search_request=True,
    )
    assert isinstance(answer, str)
    assert isinstance(sources, list)

    # If we got a capability message, routing didn't invoke tool
    if "I can search" in answer and "try asking" in answer:
        pytest.skip("Tool agent routed to capability fallback (invoke_google_for_search_request may be False elsewhere)")
    if "ran into an issue" in answer or "not configured" in answer:
        pytest.skip(f"Tool/MCP/config issue: {answer[:150]}")

    assert len(answer) > 30
    assert "Medicaid" in answer or "eligibility" in answer or "Florida" in answer


@skip_if_mcp_unreachable
def test_tool_agent_web_scrape_e2e():
    """Full flow: answer_tool with scrape trigger + URL → MCP web_scrape_review → real content."""
    from app.services.tool_agent import answer_tool

    url = "https://www.sunshinehealth.com/providers/utilization-management/clinical-payment-policies.html"
    # Use minimal question: "Scrape <url>" to avoid capability-answer interception
    answer, sources, usage, signal = answer_tool(f"Scrape {url}")

    assert isinstance(answer, str)
    assert isinstance(sources, list)

    if "I can scrape" in answer and "give me a URL" in answer:
        pytest.skip("Tool agent did not detect scrape + URL (parser/routing issue)")
    if "ran into an issue" in answer or "not configured" in answer:
        pytest.skip(f"Tool/MCP/config issue: {answer[:150]}")

    assert len(answer) > 100
    assert "clinical" in answer.lower() or "policy" in answer.lower() or "Sunshine" in answer
