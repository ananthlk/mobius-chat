"""Tool agent: answers capability questions, invokes Google search or web scrape when appropriate."""
import json
import logging
import os
import re
import urllib.request
from typing import Any

from app.services.doc_assembly import RETRIEVAL_SIGNAL_NO_SOURCES, RETRIEVAL_SIGNAL_GOOGLE_ONLY

logger = logging.getLogger(__name__)

WEB_SCRAPER_URL = os.environ.get("CHAT_SKILLS_WEB_SCRAPER_URL", "http://localhost:8002/scrape/review")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


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


def web_scrape_via_skills_api(url: str, include_summary: bool = False) -> dict[str, Any] | None:
    """Call mobius-web-scraper POST /scrape/review. Returns {text, summary} or None on failure."""
    base = (WEB_SCRAPER_URL or "").strip()
    if not base:
        logger.warning("CHAT_SKILLS_WEB_SCRAPER_URL not set; skipping web scrape")
        return None
    try:
        payload = json.dumps({"url": url, "include_summary": include_summary}).encode("utf-8")
        req = urllib.request.Request(
            base,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning("Web scrape via skills API failed: %s", e)
        return None


def answer_tool(
    question: str,
    emitter=None,
    invoke_google_for_search_request: bool = False,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Handle tool-path questions. Returns (answer_text, sources, llm_usage, retrieval_signal).

    - Capability questions ("can you search Google?"): answer from capability registry.
    - Search requests ("search Google for X"): optionally invoke Google search and answer from results.
    """
    from app.stages.agents.capabilities import get_capability_answer

    # Capability questions: answer directly
    cap_answer = get_capability_answer(question)
    if cap_answer:
        _emit(emitter, "I can answer that from what I know about my capabilities.")
        return (cap_answer, [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

    q_lower = (question or "").strip().lower()

    # Check if user wants us to scrape a URL (e.g. "scrape https://...", "scrape this url: ...")
    scrape_triggers = ("scrape", "scrape this", "scrape url", "scrape page", "scrape the")
    wants_scrape = any(t in q_lower for t in scrape_triggers)
    url = _extract_url(question or "")
    if wants_scrape and url:
        _emit(emitter, "Scraping the page...")
        result = web_scrape_via_skills_api(url, include_summary=False)
        if result:
            text = result.get("text") or ""
            summary = result.get("summary") or ""
            if not text:
                return (
                    f"I couldn't extract content from {url}. The page may be empty or block automated access.",
                    [{"document_name": url, "source_type": "external"}],
                    None,
                    RETRIEVAL_SIGNAL_NO_SOURCES,
                )
            preview = (text[:2000] + "...") if len(text) > 2000 else text
            answer = f"Here's the content from {url}:\n\n{preview}"
            if summary:
                answer += f"\n\nSummary: {summary}"
            sources = [{"index": 1, "document_name": url, "text": preview[:300], "source_type": "external"}]
            return (answer, sources, None, RETRIEVAL_SIGNAL_NO_SOURCES)
        return (
            "I tried to scrape that URL but ran into an issue. Ensure CHAT_SKILLS_WEB_SCRAPER_URL is set and mobius-web-scraper is running on port 8002.",
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

    # Check if user wants us to actually search (e.g. "search for X", "look up X on Google")
    search_triggers = ("search for", "search google for", "look up", "find information about", "google ")
    wants_search = any(t in q_lower for t in search_triggers)

    if wants_search and invoke_google_for_search_request:
        try:
            from app.services.doc_assembly import google_search_via_skills_api
            from app.services.llm_provider import get_llm_provider
            import asyncio

            # Extract search query (simplified: use question minus trigger words)
            query = question
            for t in search_triggers:
                if t in q_lower:
                    idx = q_lower.find(t)
                    query = question[idx + len(t) :].strip()
                    break
            if not query:
                query = question

            _emit(emitter, "Searching the web...")
            results = google_search_via_skills_api(query, max_results=5)
            if results:
                _emit(emitter, f"Found {len(results)} results. Summarizing...")
                context = "\n\n".join(
                    f"[{i+1}] {r.get('document_name', '')}: {r.get('text', '')[:500]}"
                    for i, r in enumerate(results[:5])
                )
                provider = get_llm_provider()
                prompt = (
                    f"Use the following web search results to answer the user's question. "
                    f"Cite sources by number [1], [2], etc.\n\n"
                    f"Results:\n{context}\n\n"
                    f"Question: {question}\n\nAnswer:"
                )
                raw, usage = asyncio.run(provider.generate_with_usage(prompt))
                answer = (raw or "").strip()
                sources = [
                    {
                        "index": i + 1,
                        "document_name": r.get("document_name", "External"),
                        "text": (r.get("text") or "")[:300],
                        "source_type": "external",
                    }
                    for i, r in enumerate(results)
                ]
                return (answer, sources, usage, RETRIEVAL_SIGNAL_GOOGLE_ONLY)
        except Exception as e:
            logger.warning("Tool agent Google search failed: %s", e)
            return (
                "I tried to search the web but ran into an issue. Please try again or rephrase.",
                [],
                None,
                RETRIEVAL_SIGNAL_NO_SOURCES,
            )

    # Fallback: capability-style answer or generic
    _emit(emitter, "This would use a tool. Let me explain what I can do.")
    return (
        "I can search the web, scrape pages, and look up provider info. "
        "For a web search, try asking something like 'Search for [topic]' or 'Look up [query]'. "
        "For policy questions about appeals, grievances, or prior auth, just ask and I'll look in our materials.",
        [],
        None,
        RETRIEVAL_SIGNAL_NO_SOURCES,
    )
