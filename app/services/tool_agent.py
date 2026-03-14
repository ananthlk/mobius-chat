"""Tool agent: answers capability questions, invokes tools via MCP.

Uses MCP manager to call skills (google_search, web_scrape_review). As we add
tools to mobius-skills-mcp, they are discovered via list_tools—no code changes.
"""
import asyncio
import logging
import re
import urllib.parse
from typing import Any

from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_NO_SOURCES,
    RETRIEVAL_SIGNAL_GOOGLE_ONLY,
    RETRIEVAL_SIGNAL_ROSTER_COMPLETE,
)
from app.services.mcp_manager import call_mcp_tool
from app.services.roster_credentialing_orchestrator import run_orchestrator

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
# For cleaning org names: remove URL-like strings including typos (hhttps, htttp) and www.domain
_URL_CLEAN_RE = re.compile(
    r"h*https?://[^\s<>\"']+|www\.[a-zA-Z0-9][-a-zA-Z0-9.]*\.[a-zA-Z]{2,}(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)

# MCP tool names (must match mobius-skills-mcp server)
TOOL_GOOGLE_SEARCH = "google_search"
TOOL_WEB_SCRAPE_REVIEW = "web_scrape_review"
TOOL_SEARCH_ORG_NAMES = "search_org_names"
TOOL_SEARCH_ORG_BY_ADDRESS = "search_org_by_address"
TOOL_HEALTHCARE_QUERY = "healthcare_query"

# ---------------------------------------------------------------------------
# Tool Isolation — Entity Extraction Utilities
# ---------------------------------------------------------------------------

# NPI: exactly 10 consecutive digits
_NPI_PATTERN = re.compile(r'\b(\d{10})\b')

# ICD-10: letter + 2 digits + optional dot + optional suffix
_ICD10_PATTERN = re.compile(r'\b([A-TV-Z][0-9][0-9AB]\.?[0-9A-TV-Z]{0,4})\b', re.I)

# Strip question scaffolding to isolate the org/entity name
_ORG_STRIP_PREFIXES = re.compile(
    r'^(what is (the )?npi (of|for)|find (the )?npi (of|for)|npi (of|for)|'
    r'search for org(anization)? name|search for org(anization)?|'  # "search for org name X"
    r'look up|search for|find|get|what are the npis? (of|for)|'
    r'providers? (at|for|in)|credentialing (report )?for|'
    r'roster (report )?for|enrollment (process )?for|'
    r'how do(es)? .{0,20} enroll with|how to enroll with|'
    r'enroll with|join|become a provider (with|at|for)|'
    r'what is .{0,10} process for|'
    r'what is the .{0,50} (for|with|of)|'  # "what is the timely filing deadline for"
    r'what (are|is) .{0,30} (deadline|requirement|process|rule)s? for)',
    re.I,
)

# Suffix patterns to strip BEFORE word filtering (tails like "and find the NPI")
_ORG_STRIP_SUFFIXES = re.compile(
    r'\s+(and\s+)?(find|get|look\s*up|retrieve|search\s+for)\s+(the\s+)?(npi|npis|npi\s+number|npi\s+numbers?)[^a-z]*$'
    r'|\s+npi\s*$',  # bare trailing "NPI"
    re.I,
)

# Words that are never the entity
_NON_ENTITY = frozenset({
    'a', 'an', 'the', 'this', 'that', 'their', 'its',
    'what', 'how', 'where', 'when', 'why', 'who', 'which',
    'provider', 'providers', 'organization', 'org', 'company',
    'network', 'medicaid', 'medicare', 'florida', 'fl',
    # Verb noise: appears when question scaffolding is only partially stripped
    'name', 'names', 'find', 'get', 'and', 'or', 'for', 'with', 'of',
    'npi', 'npis', 'number', 'numbers', 'lookup', 'search',
})

# Domains that return gating pages (login walls, aggregators, noise)
_SKIP_DOMAINS = frozenset({
    'reddit.com', 'quora.com', 'indeed.com', 'glassdoor.com',
    'yelp.com', 'facebook.com', 'linkedin.com', 'twitter.com',
    'youtube.com',
})

# Path segments that strongly indicate provider-facing content
_PROVIDER_PATH_SIGNALS = (
    'provider', 'enroll', 'credential', 'network', 'portal',
    'join', 'contract', 'participate', 'become',
)

# Content that indicates a login wall
_LOGIN_WALL_SIGNALS = (
    'sign in', 'log in', 'login required', 'please sign in',
    'create an account', 'register to', 'access denied',
)


def extract_entity_from_question(text: str) -> dict:
    """Extract the named entity the question is about — always from question text, never from ctx.

    Returns a dict with one or more of:
      npi_number: str    — if a 10-digit NPI is present
      icd10_code: str    — if an ICD-10 code is present
      org_name: str      — org/provider name
      address: str       — street address
      raw: str           — cleaned question text (fallback)
    """
    result: dict = {}
    t = (text or '').strip()

    # NPI number — highest specificity
    npi_match = _NPI_PATTERN.search(t)
    if npi_match:
        result['npi_number'] = npi_match.group(1)

    # ICD-10 code
    icd_match = _ICD10_PATTERN.search(t)
    if icd_match:
        result['icd10_code'] = icd_match.group(1).upper()

    # Address — number followed by a street word
    addr_match = re.search(
        r'\b\d+\s+[A-Z][a-z]+\s+(st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|ln|lane|way|pkwy)\b',
        t, re.I,
    )
    if addr_match:
        result['address'] = addr_match.group(0)

    # Org name — strip question scaffolding, take what remains
    stripped = _ORG_STRIP_PREFIXES.sub('', t).strip()
    # Strip trailing action phrases like "and find the NPI", "and find NPI"
    stripped = _ORG_STRIP_SUFFIXES.sub('', stripped).strip()
    stripped = re.sub(r'[?.,!]+$', '', stripped).strip()
    stripped = re.sub(
        r'\b(in florida|in fl|medicaid|medicare|provider enrollment)$',
        '', stripped, flags=re.I,
    ).strip()
    words = [w for w in stripped.split() if w.lower() not in _NON_ENTITY and len(w) > 1]
    if words:
        result['org_name'] = ' '.join(words)

    result['raw'] = t
    return result


def build_search_query(
    entity: dict,
    active: dict | None = None,
    intent: str | None = None,
) -> str:
    """Build a Google search query from extracted entity + optional jurisdiction qualifiers.

    Jurisdiction (active) is additive only — it narrows the search but NEVER replaces
    the entity. The entity always comes from the question text.
    """
    parts: list[str] = []

    # Primary: the entity being asked about
    if entity.get('npi_number'):
        return f'NPI {entity["npi_number"]}'
    if entity.get('icd10_code'):
        return f'ICD-10 {entity["icd10_code"]} diagnosis code'
    if entity.get('org_name'):
        parts.append(entity['org_name'])
    elif entity.get('address'):
        parts.append(entity['address'])
    else:
        parts.append((entity.get('raw') or '')[:80])

    # Intent hint — surfaces the specific process being asked about
    if intent:
        intent_clean = intent.lower().strip()
        if any(w in intent_clean for w in ('enroll', 'join network', 'become provider')):
            parts.append('provider enrollment')
        elif any(w in intent_clean for w in ('credenti',)):
            parts.append('credentialing requirements')
        elif any(w in intent_clean for w in ('timely', 'filing deadline')):
            parts.append('timely filing deadline')
        elif any(w in intent_clean for w in ('prior auth', 'pa requirement')):
            parts.append('prior authorization')
        elif any(w in intent_clean for w in ('appeal', 'grievance')):
            parts.append('appeals process')
        else:
            parts.append(intent_clean[:40])

    # Jurisdiction qualifiers — append only if they add specificity
    active = active or {}
    state = (active.get('jurisdiction') or active.get('state') or '').strip()
    program = (active.get('program') or '').strip()
    entity_text = ' '.join(parts).lower()
    if state and state.lower() not in entity_text:
        parts.append(state)
    if program and program.lower() not in entity_text:
        parts.append(program)

    return ' '.join(p for p in parts if p).strip()


def _score_url(url: str, entity: dict, active: dict | None) -> float:
    """Score a URL for relevance to entity + intent. Higher = more worth scraping."""
    try:
        parsed = urllib.parse.urlparse(url.lower())
        domain = parsed.netloc
        path = parsed.path
    except Exception:
        return 0.0

    for bad in _SKIP_DOMAINS:
        if bad in domain:
            return -1.0

    score = 0.0

    # Org name in domain = strong signal
    org = (entity.get('org_name') or '').lower()
    if org:
        org_slug = re.sub(r'[^a-z0-9]', '', org)[:12]
        domain_slug = re.sub(r'[^a-z0-9]', '', domain)
        if org_slug and org_slug in domain_slug:
            score += 0.5

    for signal in _PROVIDER_PATH_SIGNALS:
        if signal in path:
            score += 0.12
            break

    state = ((active or {}).get('jurisdiction') or '').lower()[:2]
    if state and f'.{state}.gov' in domain:
        score += 0.2
    if 'cms.gov' in domain or 'medicaid.gov' in domain:
        score += 0.15
    if path.endswith('.pdf'):
        score += 0.1

    depth = len([p for p in path.split('/') if p])
    if depth > 4:
        score -= 0.05 * (depth - 4)

    return score


def _parse_search_result_urls(text: str) -> list[dict]:
    """Parse structured result dicts from MCP google_search text output.

    MCP formats results as:
      [1] Title
          Snippet text
          URL: https://...
    """
    results = []
    current: dict = {}
    for line in (text or '').splitlines():
        stripped = line.strip()
        title_m = re.match(r'^\[(\d+)\]\s*(.+)$', stripped)
        if title_m:
            if current.get('url'):
                results.append(current)
            current = {'title': title_m.group(2), 'snippet': '', 'url': ''}
        elif stripped.startswith('URL:'):
            url = stripped[4:].strip()
            if url.startswith('http'):
                current['url'] = url
        elif stripped and current and not current['snippet']:
            current['snippet'] = stripped
    if current.get('url'):
        results.append(current)
    return results


def _scrape_url_simple(url: str) -> tuple[str, bool]:
    """Synchronous single-URL scrape. Returns (content, success).
    Called inside score_and_scrape_top_result() — no emitter, no extra processing.
    """
    try:
        result_text, success = call_mcp_tool(TOOL_WEB_SCRAPE_REVIEW, {"url": url, "include_summary": False})
        content = (result_text or '').strip()
        ok = success and bool(content) and len(content) > 50
        return (content, ok)
    except Exception:
        return ('', False)


def score_and_scrape_top_result(
    search_results: list[dict],
    entity: dict,
    active: dict | None,
    scrape_fn=_scrape_url_simple,
    max_attempts: int = 3,
) -> tuple[str | None, str | None, bool]:
    """Score Google search result URLs and scrape the best one.

    Returns (content, source_url, success).
    content is None if all scrape attempts fail or are login-walled.
    """
    scored: list[tuple[float, str, dict]] = []
    for r in (search_results or []):
        url = r.get('url') or r.get('link') or r.get('href') or ''
        if not url or not url.startswith('http'):
            continue
        s = _score_url(url, entity, active)
        if s > -1.0:
            scored.append((s, url, r))

    scored.sort(key=lambda x: x[0], reverse=True)

    for i, (score, url, _) in enumerate(scored[:max_attempts]):
        if score <= 0.0 and i > 0:
            break
        try:
            content, ok = scrape_fn(url)
            if not ok or not content:
                continue
            content_lower = (content or '').lower()[:500]
            if any(s in content_lower for s in _LOGIN_WALL_SIGNALS):
                logger.debug("score_and_scrape: login wall at %s, trying next", url)
                continue
            if len((content or '').strip()) < 200:
                continue
            return content, url, True
        except Exception as exc:
            logger.debug("score_and_scrape: scrape failed for %s: %s", url, exc)
            continue

    return None, None, False


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


def _extract_domain(url: str) -> str | None:
    """Extract domain from URL for search queries (e.g. https://www.lsbc.net/ -> lsbc.net)."""
    if not url or not str(url).strip():
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url.strip())
        netloc = (parsed.netloc or "").strip()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc if netloc else None
    except Exception:
        return None


def _clean_org_name_for_npi_search(raw: str, url: str | None = None) -> str:
    """Clean org name for search_org_names: strip URLs, 'whose website is X', etc.
    search_org_names only searches by name and state; URLs are not used.
    """
    s = (raw or "").strip()
    if not s:
        return s
    # Remove any URL or URL-like string (handles typos like hhttps)
    s = _URL_RE.sub("", s).strip()
    s = _URL_CLEAN_RE.sub("", s).strip()
    # Remove common trailing phrases that add context but aren't the org name
    for pattern in (
        r"\s*whose\s+website\s+is\s+.*$",
        r"\s*with\s+website\s+.*$",
        r"\s*website\s+is\s+.*$",
        r"\s*,\s*whose\s+.*$",
        r"\s*\(.*website.*\)\s*$",
    ):
        s = re.sub(pattern, "", s, flags=re.IGNORECASE).strip()
    # Trim trailing punctuation
    s = s.rstrip("?.,;:! ")
    return s


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
    user_message: str | None = None,
    extra_out: dict | None = None,
    tool_hint_override: str | None = None,
    scrape_url: str | None = None,
    question_intent: str | None = None,
    active_context: dict | None = None,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Handle tool-path questions via MCP. Returns (answer_text, sources, llm_usage, retrieval_signal).

    tool_hint_override: from planner blueprint — bypasses keyword matching.
    scrape_url: explicit URL for web_scrape (detected in resolve.py).
    question_intent: planner question_intent — used as qualifier in search query construction.
    active_context: active jurisdiction state — passed as qualifier ONLY to build_search_query(),
                    never used as a tool search target.
    """
    try:
        return _answer_tool_impl(
            question, emitter, invoke_google_for_search_request,
            user_message=user_message, extra_out=extra_out,
            tool_hint_override=tool_hint_override, scrape_url=scrape_url,
            question_intent=question_intent, active_context=active_context,
        )
    except Exception as e:
        logger.exception("tool_agent failed: %s", e)
        return (
            f"I ran into an unexpected issue. {e}. Please try again or rephrase.",
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )


def _run_web_scrape(
    url: str, emitter=None
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Scrape a URL and return (answer, sources, usage, retrieval_signal)."""
    try:
        result_text, success = call_mcp_tool(TOOL_WEB_SCRAPE_REVIEW, {"url": url, "include_summary": False})
    except Exception as e:
        logger.warning("call_mcp_tool web_scrape failed: %s", e, exc_info=True)
        return (f"I ran into an issue calling the tool. {e}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
    result_text = result_text or ""
    if success and result_text:
        preview = (result_text[:2000] + "...") if len(result_text) > 2000 else result_text
        domain = _extract_domain(url) or url[:40]
        sources = [{"index": 1, "document_name": domain, "text": preview[:300], "source_type": "web", "url": url}]
        return (preview, sources, None, RETRIEVAL_SIGNAL_GOOGLE_ONLY)
    return (
        result_text if result_text else "I tried to scrape that URL but ran into an issue. Ensure MCP server is running and CHAT_SKILLS_WEB_SCRAPER_URL is set.",
        [],
        None,
        RETRIEVAL_SIGNAL_NO_SOURCES,
    )


def _run_google_search(
    query: str,
    emitter=None,
    return_raw_results: bool = False,
) -> tuple:
    """Run a google search.

    Normal mode (return_raw_results=False):
        Returns (answer: str, sources: list[dict], usage: dict|None, signal: str)
    Raw mode (return_raw_results=True):
        Returns (raw_results: list[dict], snippets: str, usage: dict|None, signal: str)
        raw_results contains {title, snippet, url} dicts for use in score_and_scrape_top_result().
    """
    try:
        result_text, success = call_mcp_tool(TOOL_GOOGLE_SEARCH, {"query": query, "max_results": 5})
    except Exception as e:
        logger.warning("call_mcp_tool google_search failed: %s", e, exc_info=True)
        empty: list = []
        err = f"I ran into an issue calling the tool. {e}. Please try again."
        return (empty, err, None, RETRIEVAL_SIGNAL_NO_SOURCES) if return_raw_results else (err, [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

    result_text = result_text or ""
    has_results = success and result_text and "No search results found" not in result_text

    # Always parse raw URL list (used by caller when return_raw_results=True)
    raw_results = _parse_search_result_urls(result_text) if has_results else []

    if not has_results:
        msg = result_text if result_text else "I tried to search the web but ran into an issue. Ensure MCP server is running."
        return (raw_results, msg, None, RETRIEVAL_SIGNAL_NO_SOURCES) if return_raw_results else (msg, [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

    if return_raw_results:
        # Caller will handle LLM summarisation after scraping
        return (raw_results, result_text, None, RETRIEVAL_SIGNAL_GOOGLE_ONLY)

    # Normal path: LLM-summarise the snippet text
    _emit(emitter, "Found results. Summarizing...")
    try:
        from app.services.llm_provider import get_llm_provider
        provider = get_llm_provider()
        prompt = (
            f"Use the following web search results to answer the user's question. "
            f"Cite sources by number [1], [2], etc.\n\nResults:\n{result_text}\n\n"
            f"Question: {query}\n\nAnswer:"
        )
        raw, usage = asyncio.run(provider.generate_with_usage(prompt))
        answer = (raw or "").strip()
        sources = [{"index": 1, "document_name": "Web search", "text": result_text[:300], "source_type": "external"}]
        return (answer, sources, usage, RETRIEVAL_SIGNAL_GOOGLE_ONLY)
    except Exception as e:
        logger.warning("LLM summarization failed, using raw results: %s", e)
        return (result_text, [{"document_name": "Web search", "source_type": "external"}], None, RETRIEVAL_SIGNAL_GOOGLE_ONLY)


def _clean_org_name_for_search(name: str) -> str:
    """Strip any residual NPI-lookup noise from an org name before sending to NPPES search.
    Handles cases where entity extraction returned partially-cleaned text.
    """
    s = (name or "").strip()
    # Strip leading noise words that slipped through (e.g. "name David Lawrence Center")
    s = re.sub(
        r'^(org(anization)?\s+)?name\s+',
        '', s, flags=re.I,
    ).strip()
    # Strip trailing "and find (the) NPI" / bare "NPI"
    s = _ORG_STRIP_SUFFIXES.sub('', s).strip()
    s = re.sub(r'\s+(npi|npis|npi\s+number)s?\s*$', '', s, flags=re.I).strip()
    return s


def _run_npi_lookup_by_name(
    org_name: str,
    emitter=None,
    extract_candidate: str = "",
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Look up NPI(s) for a named org. Returns confidence-ranked results.

    Single exact match → direct answer with NPI.
    Multiple matches  → confidence display with clarification prompt.
    No match          → falls back to Google search.
    """
    # Final defensive clean in case entity extraction returned partially-noisy text
    org_name = _clean_org_name_for_search(org_name)
    if not org_name or len(org_name) < 2:
        return ("I need an organization name to look up NPIs. Try: 'What is the NPI for [org name]?'", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

    url_in_text = _extract_url(extract_candidate)
    try:
        result_text, success = call_mcp_tool(
            TOOL_SEARCH_ORG_NAMES,
            {"name": org_name, "state": "FL", "limit": 10},
        )
    except Exception as e:
        logger.warning("call_mcp_tool search_org_names failed: %s", e, exc_info=True)
        return (f"I ran into an issue looking up NPIs. {e}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

    # The MCP tool now returns the confidence-formatted text directly.
    # "No matches found" means we should fall through to Google.
    org_search_found = (
        success and result_text
        and "Error:" not in result_text
        and "No matches found" not in (result_text or "")
    )
    if org_search_found:
        sources = [{"index": 1, "document_name": "NPPES / PML search", "text": result_text[:300], "source_type": "external"}]
        return (result_text, sources, None, RETRIEVAL_SIGNAL_NO_SOURCES)
    # Fallback: Google search for NPI
    domain = _extract_domain(url_in_text) if url_in_text else None
    if not domain and extract_candidate:
        m = re.search(r"(?:h*https?://)?(?:www\.)?([a-zA-Z0-9][-a-zA-Z0-9.]*\.[a-zA-Z]{2,})", extract_candidate, re.IGNORECASE)
        domain = m.group(1) if m else None
    google_query = f"{org_name} NPI" + (f" {domain}" if domain else "")
    _emit(emitter, "No direct NPI match. Searching the web…")
    try:
        google_text, google_ok = call_mcp_tool(TOOL_GOOGLE_SEARCH, {"query": google_query, "max_results": 5})
    except Exception as e:
        logger.warning("call_mcp_tool google_search fallback failed: %s", e)
        return (
            result_text if result_text else f"No NPIs found for '{org_name}'. Try the exact legal name or an address.",
            [], None, RETRIEVAL_SIGNAL_NO_SOURCES,
        )
    if google_ok and google_text and "No search results found" not in (google_text or ""):
        try:
            from app.services.llm_provider import get_llm_provider
            provider = get_llm_provider()
            prompt = (
                f"The user asked for the NPI of {org_name}."
                + (f" They mentioned the website/domain: {domain}." if domain else "")
                + f"\n\nWeb search results:\n{google_text}\n\n"
                "Extract and state any NPI numbers found (10 digits). If multiple NPIs exist, list them with context. "
                "If no NPI is found, say so clearly. Keep the answer concise."
            )
            raw, usage = asyncio.run(provider.generate_with_usage(prompt))
            answer = (raw or "").strip()
            sources = [{"index": 1, "document_name": "Web search", "text": (google_text or "")[:300], "source_type": "external"}]
            return (answer, sources, usage, RETRIEVAL_SIGNAL_GOOGLE_ONLY)
        except Exception as e:
            logger.warning("LLM synthesis of Google NPI results failed: %s", e)
            return (
                (google_text or "")[:1500] + "\n\n(I couldn't synthesize a clean answer; above are web search snippets.)",
                [{"document_name": "Web search", "source_type": "external"}],
                None,
                RETRIEVAL_SIGNAL_GOOGLE_ONLY,
            )
    return (
        result_text if result_text else f"No NPIs found for '{org_name}' in our database or via web search. Try the exact legal name or an address.",
        [], None, RETRIEVAL_SIGNAL_NO_SOURCES,
    )


def _run_npi_by_address(
    address: str,
    emitter=None,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Look up NPI(s) at a physical address via search_org_by_address MCP tool."""
    _emit(emitter, "Searching for providers at that address…")
    try:
        result_text, success = call_mcp_tool(
            TOOL_SEARCH_ORG_BY_ADDRESS,
            {"address_raw": address, "state": "FL", "limit": 10},
        )
    except Exception as e:
        logger.warning("call_mcp_tool search_org_by_address failed: %s", e, exc_info=True)
        return (f"I ran into an issue with the address lookup. {e}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
    if success and result_text and "Error:" not in result_text and "No matches found" not in result_text:
        sources = [{"index": 1, "document_name": "Address lookup", "text": result_text[:300], "source_type": "external"}]
        return (result_text, sources, None, RETRIEVAL_SIGNAL_NO_SOURCES)
    return (
        result_text if result_text else f"No providers found at '{address}'. Try a more specific address.",
        [], None, RETRIEVAL_SIGNAL_NO_SOURCES,
    )


def _answer_tool_impl(
    question: str,
    emitter=None,
    invoke_google_for_search_request: bool = False,
    user_message: str | None = None,
    extra_out: dict | None = None,
    tool_hint_override: str | None = None,
    scrape_url: str | None = None,
    question_intent: str | None = None,
    active_context: dict | None = None,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Implementation of answer_tool. When user_message is set, roster triggers and org name use user_message.

    Tool Isolation Principle: active_context (jurisdiction) is used ONLY as a query qualifier
    in build_search_query(). It is never the search target. Entity tools extract their target
    exclusively from question text via extract_entity_from_question().
    """
    from app.stages.agents.capabilities import get_capability_answer

    # ── Intent-based dispatch (from planner blueprint) ────────────────────
    # tool_hint_override bypasses keyword matching entirely. Uses entity extraction
    # so active jurisdiction NEVER bleeds into tool search targets.
    if tool_hint_override:
        hint = tool_hint_override.lower().strip()

        # Extract entity from question text — ALWAYS from question, never from active_context
        entity = extract_entity_from_question(text=(user_message or question or ""))
        active = active_context or {}

        if hint == "web_scrape":
            url = scrape_url
            if not url:
                url = _extract_url(question or "") or _extract_url(user_message or "")
            if url:
                return _run_web_scrape(url, emitter=emitter)
            hint = "google_search"  # no URL — fall through to search

        if hint == "google_search":
            query = build_search_query(entity, active, intent=question_intent)
            if not query.strip():
                query = (question or "").strip()
            # Fetch raw results, then auto-scrape the best URL
            raw_results, snippets, usage, signal = _run_google_search(
                query, emitter=emitter, return_raw_results=True,
            )
            content, source_url, ok = score_and_scrape_top_result(
                raw_results, entity, active,
            )
            if ok and content:
                domain = _extract_domain(source_url) or (source_url or "")[:40]
                sources = [{"url": source_url, "source_type": "web", "document_name": domain}]
                return (content[:4000], sources, usage, RETRIEVAL_SIGNAL_GOOGLE_ONLY)
            # Scrape failed — LLM-summarise snippets
            if snippets and "No search results" not in snippets:
                _emit(emitter, "Summarizing search results...")
                try:
                    from app.services.llm_provider import get_llm_provider
                    provider = get_llm_provider()
                    prompt = (
                        f"Use the following web search results to answer the user's question. "
                        f"Cite sources by number [1], [2], etc.\n\nResults:\n{snippets}\n\n"
                        f"Question: {question}\n\nAnswer:"
                    )
                    raw_ans, llm_usage = asyncio.run(provider.generate_with_usage(prompt))
                    answer = (raw_ans or "").strip()
                    disclaimer = "\n\n[Note: Based on search result summaries. Verify against source pages.]"
                    return (
                        answer + disclaimer,
                        [{"document_name": "Web search", "source_type": "external"}],
                        llm_usage,
                        RETRIEVAL_SIGNAL_GOOGLE_ONLY,
                    )
                except Exception as e:
                    logger.warning("LLM summarization of search snippets failed: %s", e)
                    return (
                        snippets + "\n\n[Note: These are search result summaries.]",
                        [{"document_name": "Web search", "source_type": "external"}],
                        None,
                        RETRIEVAL_SIGNAL_GOOGLE_ONLY,
                    )
            return (snippets or "No results found.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

        if hint in ("npi_lookup", "search_org_names"):
            # Entity from question ONLY — active payer is never the org being looked up
            org = entity.get('org_name') or entity.get('raw', '')[:80]
            if org and len(org.strip()) > 1:
                return _run_npi_lookup_by_name(org.strip(), emitter=emitter,
                                               extract_candidate=(user_message or question or ""))
            # No extractable org — fall through to keyword path

        if hint == "search_org_by_address":
            addr = entity.get('address') or entity.get('raw', '')[:80]
            if addr and len(addr.strip()) > 3:
                return _run_npi_by_address(addr.strip(), emitter=emitter)
            # No extractable address — fall through to keyword path

        if hint == "healthcare_query":
            npi = entity.get('npi_number')
            icd = entity.get('icd10_code')
            state = active.get('jurisdiction', '') or active.get('state', '')
            hc_question = npi or icd or entity.get('raw', '')[:120]
            try:
                result_text, success = call_mcp_tool(
                    TOOL_HEALTHCARE_QUERY,
                    {"question": hc_question},
                )
            except Exception as e:
                logger.warning("call_mcp_tool healthcare_query (hint) failed: %s", e, exc_info=True)
                return (f"I ran into an issue. {e}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
            if success and result_text and "Error:" not in result_text:
                sources = [{"index": 1, "document_name": "Healthcare lookup", "text": result_text[:300], "source_type": "external"}]
                return (result_text, sources, None, RETRIEVAL_SIGNAL_NO_SOURCES)
            return (
                result_text if result_text else "Healthcare lookup failed. Ensure mobius-healthcare API is running.",
                [],
                None,
                RETRIEVAL_SIGNAL_NO_SOURCES,
            )

        if hint == "roster_report":
            org = entity.get('org_name') or entity.get('raw', '')[:80]
            if org and len(org.strip()) > 1:
                # Fall through to keyword path with extracted org name pre-computed
                pass
    # ── Existing keyword-based dispatch continues below ───────────────────

    q_lower = (question or "").strip().lower()
    roster_check_text = (user_message or question or "").strip()
    roster_lower = roster_check_text.lower()
    # For org search: use subquestion first; fall back to user_message when planner reframes (e.g. "Search for Lifestream NPI")
    # so we can still extract org name from original question
    extract_text = (question or "").strip()
    extract_lower = extract_text.lower()
    extract_candidate = (user_message or question or "").strip()  # fallback for org extraction

    # Actionable requests first: scrape+URL and search+invoke bypass capability-answer
    url = _extract_url(question or "") or _extract_url(user_message or "")

    # Scrape: "scrape https://...", "scrape this url: ..."
    scrape_triggers = ("scrape", "scrape this", "scrape url", "scrape page", "scrape the")
    wants_scrape = any(t in q_lower for t in scrape_triggers)
    if wants_scrape and url:
        return _run_web_scrape(url, emitter=emitter)
    if wants_scrape and not url:
        return (
            "I can scrape web pages when you give me a URL. Try: 'Scrape https://example.com' or paste the URL.",
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    # Provider Roster / Credentialing report: "provider roster for X", "credentialing report for X",
    # "create a Medicaid NPI report for X", "I want to create a credentialing report for X"
    roster_triggers = (
        "provider roster",
        "credentialing report",
        "roster report",
        "medicaid roster",
        "roster for",
        "medicaid npi report",
        "create a medicaid npi report",
        "create medicaid npi report",
        "create a credentialing report",
        "create credentialing report",
        "i want to create a medicaid npi report",
        "i want to create a credentialing report",
    )
    wants_roster = any(t in roster_lower for t in roster_triggers)
    if wants_roster:
        org_name = roster_check_text
        for t in (
            "provider roster for",
            "credentialing report for",
            "roster report for",
            "medicaid roster for",
            "roster for",
            "create a medicaid npi report for",
            "create medicaid npi report for",
            "create a credentialing report for",
            "create credentialing report for",
            "i want to create a medicaid npi report for",
            "i want to create a credentialing report for",
            "medicaid npi report for",
        ):
            if t in roster_lower:
                org_name = roster_check_text[roster_lower.find(t) + len(t) :].strip()
                break
        if org_name and len(org_name) > 1:
            _emit(emitter, f"Running the Medicaid NPI report for {org_name}…")
            try:
                result_text, ostate = run_orchestrator(org_name, emitter=emitter)
            except Exception as e:
                logger.warning("run_orchestrator failed: %s", e, exc_info=True)
                return (f"I ran into an issue running the plan. {e}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
            result_text = result_text or ""
            if extra_out is not None:
                step_order = {
                    "ensure_benchmarks": 1,
                    "identify_org": 2,
                    "find_locations": 3,
                    "find_associated_providers": 4,
                    "org_benchmark": 5,
                    "find_services_by_location": 6,
                    "historic_billing_patterns": 7,
                    "step_6": 8,
                    "step_7": 9,
                    "opportunity_sizing": 10,
                    "build_report": 11,
                }
                extra_out["roster_step_outputs"] = [
                    {
                        "step_id": s.step_id,
                        "step_num": step_order.get(s.step_id, 0),
                        "label": s.label,
                        "csv_content": s.csv_content,
                        "row_count": s.row_count,
                    }
                    for s in (ostate.step_outputs or [])
                ]
                if getattr(ostate, "report_pdf_base64", None):
                    extra_out["roster_report_pdf_base64"] = ostate.report_pdf_base64
                if getattr(ostate, "report_final_md", None):
                    extra_out["roster_report_final_md"] = ostate.report_final_md
            if result_text:
                sources = [{"index": 1, "document_name": "Provider Roster / Credentialing", "text": result_text[:300], "source_type": "external"}]
                return (result_text, sources, None, RETRIEVAL_SIGNAL_ROSTER_COMPLETE)
            return ("Report could not be generated. Ensure provider-roster-credentialing API is running and CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL is set.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
        return (
            "I can run a Provider Roster / Credentialing (Medicaid NPI) report for an organization. Try: 'Create a Medicaid NPI report for David Lawrence' or 'Credentialing report for Aspire'.",
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    # Org search: "what is the npi of X", "npis for X", "find npi for X"
    # Use subquestion first; fall back to user_message when planner reframes (e.g. "Search for Lifestream NPI")
    org_search_triggers = (
        "npi of ",
        "npis for ",
        "npi for ",
        "what is the npi of ",
        "what are the npis for ",
        "find npi for ",
        "find the npi for ",
        "find npis for ",
        "look up npi for ",
        "look up npis for ",
    )

    def _extract_org_from(txt: str) -> str | None:
        if not txt or len(txt) < 2:
            return None
        lower = txt.strip().lower()
        for t in org_search_triggers:
            if t in lower:
                raw = txt[lower.find(t) + len(t) :].strip().rstrip("?.,;!")
                cleaned = _clean_org_name_for_npi_search(raw, _extract_url(txt) if txt else None)
                # "Lifestream NPI" → cleaned keeps "Lifestream" (NPI is stripped or kept; _clean doesn't strip "NPI")
                # Actually "Lifestream NPI" - no URL, no "whose website" - we'd get "Lifestream NPI". Org search
                # with "Lifestream NPI" would use word AND: lifestream AND npi. Org names might not have "npi".
                # So we should strip trailing " NPI" / " npis" when cleaning.
                if cleaned and len(cleaned) > 1:
                    # Strip trailing " NPI" / " npis" (common reframe artifact)
                    for suffix in (" npi", " npis", " npi number", " npi numbers"):
                        if cleaned.lower().endswith(suffix):
                            cleaned = cleaned[: -len(suffix)].strip()
                            break
                return cleaned if cleaned and len(cleaned) > 1 else None
        return None

    org_name = _extract_org_from(extract_text) or (_extract_org_from(extract_candidate) if extract_candidate != extract_text else None)
    if org_name and len(org_name) > 1:
        return _run_npi_lookup_by_name(org_name, emitter=emitter, extract_candidate=extract_candidate)

    # Healthcare query: NPI lookup by number, ICD-10, CMS coverage — use subquestion text
    healthcare_triggers = (
        "icd-10",
        "icd10",
        "look up npi ",
        "npi lookup",
        "npi number ",
        "medicare coverage",
        "medicaid coverage",
        "ncd ",
        "lcd ",
        "prior auth",
        "diagnosis code",
    )
    wants_healthcare = any(t in extract_lower for t in healthcare_triggers)
    if wants_healthcare:
        try:
            result_text, success = call_mcp_tool(
                TOOL_HEALTHCARE_QUERY,
                {"question": extract_text},
            )
        except Exception as e:
            logger.warning("call_mcp_tool healthcare_query failed: %s", e, exc_info=True)
            return (f"I ran into an issue. {e}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
        if success and result_text and "Error:" not in result_text:
            sources = [{"index": 1, "document_name": "Healthcare lookup", "text": result_text[:300], "source_type": "external"}]
            return (result_text, sources, None, RETRIEVAL_SIGNAL_NO_SOURCES)
        return (
            result_text if result_text else "Healthcare lookup failed. Ensure mobius-healthcare API is running (port 8007) and CHAT_SKILLS_HEALTHCARE_URL is set.",
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
        return _run_google_search(query, emitter=emitter)

    # Fallback: capability-style answer
    _emit(emitter, "This would use a tool. Let me explain what I can do.")
    return (
        "I can search the web, scrape pages, look up provider NPIs, and answer healthcare questions. "
        "Try: 'What is the NPI of [org name]', 'What does ICD-10 Z00.00 mean?', 'Look up NPI 1234567890', or 'Search for [topic]'. "
        "For policy questions about appeals, grievances, or prior auth, just ask and I'll look in our materials.",
        [],
        None,
        RETRIEVAL_SIGNAL_NO_SOURCES,
    )
