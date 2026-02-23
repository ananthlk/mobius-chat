"""Tool agent: answers capability questions, invokes tools via MCP.

Uses MCP manager to call skills (google_search, web_scrape_review). As we add
tools to mobius-skills-mcp, they are discovered via list_tools—no code changes.
"""
import asyncio
import logging
import re
from typing import Any

from app.services.doc_assembly import RETRIEVAL_SIGNAL_NO_SOURCES, RETRIEVAL_SIGNAL_GOOGLE_ONLY
from app.services.mcp_manager import call_mcp_tool

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# MCP tool names (must match mobius-skills-mcp server)
TOOL_GOOGLE_SEARCH = "google_search"
TOOL_WEB_SCRAPE_REVIEW = "web_scrape_review"


def _emit(emitter, msg: str) -> None:
    try:
        if emitter and msg and str(msg).strip():
            emitter(str(msg).strip())
    except Exception:
        pass


def _extract_url(text: str) -> str | None:
    """Extract first URL from text."""
    m = _URL_RE.search(text)
    return m.group(0) if m else None


def _extract_search_query(question: str) -> str:
    """Extract search query from question by stripping trigger phrases."""
    q_lower = (question or "").strip().lower()
    search_triggers = ("search the web", "search google for", "search for", "look up", "find information about", "google ")
    for t in search_triggers:
        if t in q_lower:
            idx = q_lower.find(t)
            return (question or "")[idx + len(t) :].strip()
    return (question or "").strip()


def answer_tool(
    question: str,
    emitter=None,
    invoke_google_for_search_request: bool = False,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Handle tool-path questions via MCP. Returns (answer_text, sources, llm_usage, retrieval_signal)."""
    try:
        return _answer_tool_impl(question, emitter, invoke_google_for_search_request)
    except Exception as e:
        logger.exception("tool_agent failed: %s", e)
        return (
            f"I ran into an unexpected issue. {e}. Please try again or rephrase.",
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )


def _answer_tool_impl(
    question: str,
    emitter=None,
    invoke_google_for_search_request: bool = False,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Implementation of answer_tool. Call answer_tool for the safe wrapper."""
    from app.stages.agents.capabilities import get_capability_answer

    q_lower = (question or "").strip().lower()

    # Actionable requests first: scrape+URL and search+invoke bypass capability-answer
    url = _extract_url(question or "")

    # Scrape: "scrape https://...", "scrape this url: ..."
    scrape_triggers = ("scrape", "scrape this", "scrape url", "scrape page", "scrape the")
    wants_scrape = any(t in q_lower for t in scrape_triggers)
    if wants_scrape and url:
        _emit(emitter, "Scraping the page...")
        try:
            result_text, success = call_mcp_tool(TOOL_WEB_SCRAPE_REVIEW, {"url": url, "include_summary": False})
        except Exception as e:
            logger.warning("call_mcp_tool failed: %s", e, exc_info=True)
            return (f"I ran into an issue calling the tool. {e}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
        result_text = result_text or ""
        if success and result_text:
            preview = (result_text[:2000] + "...") if len(result_text) > 2000 else result_text
            sources = [{"index": 1, "document_name": url, "text": preview[:300], "source_type": "external"}]
            return (preview, sources, None, RETRIEVAL_SIGNAL_NO_SOURCES)
        return (
            result_text if result_text else "I tried to scrape that URL but ran into an issue. Ensure MCP server is running and CHAT_SKILLS_WEB_SCRAPER_URL is set.",
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )
    if wants_scrape and not url:
        return (
            "I can scrape web pages when you give me a URL. Try: 'Scrape https://example.com' or paste the URL.",
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    # Capability questions (when no actionable scrape/search): answer directly
    cap_answer = get_capability_answer(question)
    if cap_answer:
        _emit(emitter, "I can answer that from what I know about my capabilities.")
        return (cap_answer, [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

    # Search: "search for X", "look up X", etc. (actionable when invoke_google_for_search_request)
    search_triggers = ("search the web", "search google for", "search for", "look up", "find information about", "google ")
    wants_search = any(t in q_lower for t in search_triggers)

    if wants_search and invoke_google_for_search_request:
        query = _extract_search_query(question)
        if not query:
            query = question.strip()
        _emit(emitter, "Searching the web...")
        try:
            result_text, success = call_mcp_tool(TOOL_GOOGLE_SEARCH, {"query": query, "max_results": 5})
        except Exception as e:
            logger.warning("call_mcp_tool failed: %s", e, exc_info=True)
            return (f"I ran into an issue calling the tool. {e}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
        result_text = result_text or ""
        if success and result_text and "No search results found" not in result_text:
            _emit(emitter, "Found results. Summarizing...")
            try:
                from app.services.llm_provider import get_llm_provider

                provider = get_llm_provider()
                prompt = (
                    f"Use the following web search results to answer the user's question. "
                    f"Cite sources by number [1], [2], etc.\n\n"
                    f"Results:\n{result_text}\n\n"
                    f"Question: {question}\n\nAnswer:"
                )
                raw, usage = asyncio.run(provider.generate_with_usage(prompt))
                answer = (raw or "").strip()
                sources = [{"index": 1, "document_name": "Web search", "text": result_text[:300], "source_type": "external"}]
                return (answer, sources, usage, RETRIEVAL_SIGNAL_GOOGLE_ONLY)
            except Exception as e:
                logger.warning("LLM summarization failed, using raw results: %s", e)
                return (result_text, [{"document_name": "Web search", "source_type": "external"}], None, RETRIEVAL_SIGNAL_GOOGLE_ONLY)
        return (
            result_text if result_text else "I tried to search the web but ran into an issue. Ensure MCP server is running (mobius-skills-mcp on port 8006) and CHAT_SKILLS_GOOGLE_SEARCH_URL is set.",
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    # Fallback: capability-style answer
    _emit(emitter, "This would use a tool. Let me explain what I can do.")
    return (
        "I can search the web, scrape pages, and look up provider info. "
        "For a web search, try asking something like 'Search for [topic]' or 'Look up [query]'. "
        "For policy questions about appeals, grievances, or prior auth, just ask and I'll look in our materials.",
        [],
        None,
        RETRIEVAL_SIGNAL_NO_SOURCES,
    )
