"""Tool agent: answers capability questions, invokes tools via MCP.

Uses MCP manager to call skills (google_search, web_scrape_review). As we add
tools to mobius-skills-mcp, they are discovered via list_tools—no code changes.
"""
import asyncio
import logging
import re
import urllib.parse
from typing import Any

import httpx

from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_NO_SOURCES,
    RETRIEVAL_SIGNAL_GOOGLE_ONLY,
)
from app.services.mcp_manager import call_mcp_tool


logger = logging.getLogger(__name__)

# ReAct / reasoning: short line for parsers; full markdown remains the tool ``result`` string.
# When merged into one assistant string, use ``compose_mobius_tool_envelope`` (Summary = internal,
# Detail = user display & download) — see ``app.communication.tool_output_envelope``.
REACT_TOOL_SUMMARY_KEY = "react_tool_summary"


def _react_summary_from_long_markdown(text: str, *, heading: str, max_chars: int = 600) -> str:
    """First-line / head trim for NPPES lookup, reports, healthcare — keeps ReAct context small."""
    t = (text or "").strip()
    if not t:
        return ""
    if len(t) <= max_chars:
        return t
    head = t[:max_chars]
    cut = head.rfind("\n\n")
    if cut > 200:
        head = head[:cut]
    return f"{heading}\n\n{head.strip()}\n\n*(Full output is in the markdown detail block in the user message / tool result.)*"


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

# Web scrape modes — keep in sync with mobius-skills-mcp web_scrape_review (scrape_mode + limits).
WEB_SCRAPE_MODE_QUICK = "quick"
WEB_SCRAPE_MODE_MEDIUM = "medium"
WEB_SCRAPE_MODE_DETAILED = "detailed"
WEB_SCRAPE_MODE_SPECS: dict[str, dict[str, int]] = {
    WEB_SCRAPE_MODE_QUICK: {"max_depth": 1, "max_pages": 1, "max_doc_downloads": 0},
    WEB_SCRAPE_MODE_MEDIUM: {"max_depth": 3, "max_pages": 6, "max_doc_downloads": 0},
    WEB_SCRAPE_MODE_DETAILED: {"max_depth": 5, "max_pages": 50, "max_doc_downloads": 10},
}
_WEB_SCRAPE_RESULT_CAP = {WEB_SCRAPE_MODE_QUICK: 8000, WEB_SCRAPE_MODE_MEDIUM: 32000, WEB_SCRAPE_MODE_DETAILED: 120000}
_WEB_SCRAPE_MCP_TIMEOUT = {WEB_SCRAPE_MODE_QUICK: 45.0, WEB_SCRAPE_MODE_MEDIUM: 120.0, WEB_SCRAPE_MODE_DETAILED: 300.0}


def normalize_web_scrape_mode(mode: str | None) -> str:
    """Map caller input to quick | medium | detailed (default quick)."""
    m = (mode or "").strip().lower()
    if not m or m in ("quick", "fast", "single", "1"):
        return WEB_SCRAPE_MODE_QUICK
    if m in ("medium", "standard", "tree"):
        return WEB_SCRAPE_MODE_MEDIUM
    if m in ("detailed", "deep", "full", "thorough"):
        return WEB_SCRAPE_MODE_DETAILED
    if m in WEB_SCRAPE_MODE_SPECS:
        return m
    return WEB_SCRAPE_MODE_QUICK


def web_scrape_review_mcp_arguments(
    url: str,
    *,
    include_summary: bool = False,
    scrape_mode: str | None = None,
) -> dict[str, Any]:
    """Arguments for MCP tool web_scrape_review (and matching scraper HTTP API body)."""
    mode = normalize_web_scrape_mode(scrape_mode)
    spec = WEB_SCRAPE_MODE_SPECS[mode]
    return {
        "url": url,
        "include_summary": bool(include_summary),
        "scrape_mode": mode,
        "max_depth": spec["max_depth"],
        "max_pages": spec["max_pages"],
        "max_doc_downloads": spec["max_doc_downloads"],
    }
TOOL_SEARCH_ORG_BY_ADDRESS = "search_org_by_address"
TOOL_HEALTHCARE_QUERY = "healthcare_query"
TOOL_ORG_NPI_LOOKUP = "org_npi_lookup"

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

# ── Auto-scrape constants ────────────────────────────────────────────────────

# Domains never worth scraping for payer policy content
_SKIP_DOMAINS = frozenset({
    'reddit.com', 'quora.com', 'indeed.com', 'glassdoor.com',
    'yelp.com', 'facebook.com', 'linkedin.com', 'twitter.com',
    'youtube.com', 'instagram.com', 'tiktok.com', 'pinterest.com',
})

# Path segments that indicate provider-facing / policy content
_PROVIDER_PATH_SIGNALS = (
    'provider', 'enroll', 'credential', 'network', 'portal',
    'prior-auth', 'authorization', 'prior_auth', 'pa-criteria',
    'formulary', 'billing', 'claims', 'medicaid', 'coverage',
    'become-a-provider', 'join-network',
    'join', 'contract', 'participate', 'become',
)

# Content that indicates scrape hit a login wall
_LOGIN_WALL_SIGNALS = (
    'sign in to continue', 'log in to continue',
    'login required', 'please sign in',
    'create an account to', 'register to view',
    'access denied', 'you must be logged in',
    'session has expired', 'please log back in',
    'sign in', 'log in', 'create an account', 'register to',
)

# Direct scrape settings
_DIRECT_SCRAPE_TIMEOUT = 8.0  # seconds
_DIRECT_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; MobiusBot/1.0; "
        "+https://mobiushealth.ai/bot)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

# Minimum content length to consider a scrape successful
_MIN_CONTENT_LENGTH = {
    'policy':  500,   # PA criteria, coverage, prior auth docs
    'portal':  300,   # Provider portal pages
    'default': 200,   # Everything else
}

# Maximum content length passed to integrator LLM (token budget)
_MAX_CONTENT_LENGTH = 8000  # characters


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


def _score_url(
    url: str,
    org_name: str | None = None,
    state: str | None = None,
) -> float:
    """Score a URL for scrape worthiness. Returns -1.0 to skip, 0.0-1.5+ otherwise."""
    try:
        parsed = urllib.parse.urlparse(url.lower())
        domain = parsed.netloc.replace('www.', '')
        path = parsed.path
    except Exception:
        return 0.0

    # Hard skip — known noise domains
    for bad in _SKIP_DOMAINS:
        if bad in domain:
            return -1.0

    # Hard skip — third-party NPI aggregators (noise for policy questions)
    for agg in ('npiprofile.com', 'npinumberlookup.org',
                'medicarelist.com', 'opennpi.com', 'npidb.org'):
        if agg in domain:
            return -1.0

    score = 0.0

    # Org name in domain — strongest signal (e.g. sunshinehealth.com for 'Sunshine Health')
    if org_name:
        slug = re.sub(r'[^a-z0-9]', '', org_name.lower())[:12]
        domain_slug = re.sub(r'[^a-z0-9]', '', domain)
        if slug and len(slug) > 3 and slug in domain_slug:
            score += 0.6

    # Provider-facing path keywords
    for signal in _PROVIDER_PATH_SIGNALS:
        if signal in path:
            score += 0.15
            break  # count once

    # Government / CMS sources
    if '.gov' in domain:
        score += 0.2
    if 'cms.gov' in domain or 'medicaid.gov' in domain:
        score += 0.1

    # State Medicaid agency
    if state:
        state_slug = (state or '').lower()[:2]
        if state_slug and (f'.{state_slug}.gov' in domain or f'ahca.my{state_slug}' in domain):
            score += 0.15

    # PDF — often the actual policy document
    if path.endswith('.pdf'):
        score += 0.1

    # Penalise very deep paths (likely old/buried content)
    depth = len([p for p in path.split('/') if p])
    if depth > 5:
        score -= 0.05 * (depth - 5)

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


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text. No external dependencies."""
    if not html:
        return ''
    # Remove <script>, <style>, <noscript> blocks entirely
    text = re.sub(
        r'<(script|style|noscript)[^>]*>.*?</(script|style|noscript)>',
        ' ', html, flags=re.DOTALL | re.IGNORECASE,
    )
    # Remove <head> block
    text = re.sub(
        r'<head[^>]*>.*?</head>',
        ' ', text, flags=re.DOTALL | re.IGNORECASE,
    )
    # Replace block elements with newlines for readability
    text = re.sub(
        r'<(br|p|div|h[1-6]|li|tr|section|article)[^>]*>',
        '\n', text, flags=re.IGNORECASE,
    )
    # Remove all remaining HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode common HTML entities
    _ENTITIES = {
        '&amp;': '&', '&lt;': '<', '&gt;': '>',
        '&nbsp;': ' ', '&quot;': '"', '&#39;': "'",
        '&mdash;': '\u2014', '&ndash;': '\u2013',
        '&hellip;': '...', '&copy;': '(c)', '&reg;': '(R)',
    }
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    # Decode numeric entities e.g. &#160; &#8217;
    def _decode_numeric(m: re.Match) -> str:
        try:
            return chr(int(m.group(1)))
        except (ValueError, OverflowError):
            return ''
    text = re.sub(r'&#(\d+);', _decode_numeric, text)
    # Collapse whitespace, preserve single newlines as paragraph breaks
    lines = []
    for line in text.split('\n'):
        line = re.sub(r'[ \t]+', ' ', line).strip()
        if line:
            lines.append(line)
    return '\n'.join(lines)[:_MAX_CONTENT_LENGTH]


def _scrape_direct(url: str) -> tuple[str, bool]:
    """Fast-path direct HTTP scrape. Handles public static HTML pages.
    Returns (plain_text_content, success). Returns ('', False) for PDFs, login walls, errors.
    """
    try:
        response = httpx.get(
            url,
            headers=_DIRECT_SCRAPE_HEADERS,
            timeout=_DIRECT_SCRAPE_TIMEOUT,
            follow_redirects=True,
        )
        if response.status_code != 200:
            return '', False
        content_type = response.headers.get('content-type', '').lower()
        # PDF — let MCP handle it
        if 'pdf' in content_type or url.lower().endswith('.pdf'):
            return '', False
        # Non-HTML types we can't parse
        if content_type and not any(t in content_type for t in ('html', 'text', 'xml')):
            return '', False
        text = _html_to_text(response.text)
        if not text or len(text.strip()) < 100:
            return '', False
        # Login wall check on first 800 chars
        text_lower = text.lower()[:800]
        if any(s in text_lower for s in _LOGIN_WALL_SIGNALS):
            return '', False
        # Content length check by page type
        path = urllib.parse.urlparse(url).path.lower()
        if any(w in path for w in ('auth', 'criteria', 'policy', 'coverage', 'formulary')):
            min_len = _MIN_CONTENT_LENGTH['policy']
        elif 'provider' in path:
            min_len = _MIN_CONTENT_LENGTH['portal']
        else:
            min_len = _MIN_CONTENT_LENGTH['default']
        if len(text.strip()) < min_len:
            return '', False
        return text.strip(), True
    except httpx.TimeoutException:
        return '', False
    except httpx.ConnectError:
        return '', False
    except Exception:
        return '', False


def _scrape_via_mcp(url: str) -> tuple[str, bool]:
    """MCP web_scrape_review fallback — handles JS-rendered pages, PDFs, blocked agents.
    Wired to the same call_mcp_tool(TOOL_WEB_SCRAPE_REVIEW, ...) used elsewhere.
    Returns (content, success).
    """
    try:
        args = web_scrape_review_mcp_arguments(url, include_summary=False, scrape_mode=WEB_SCRAPE_MODE_QUICK)
        result_text, success = call_mcp_tool(
            TOOL_WEB_SCRAPE_REVIEW,
            args,
            read_timeout=_WEB_SCRAPE_MCP_TIMEOUT[WEB_SCRAPE_MODE_QUICK],
        )
        content = (result_text or '').strip()
        if not content or len(content) < 200:
            return '', False
        content_lower = content.lower()[:800]
        if any(s in content_lower for s in _LOGIN_WALL_SIGNALS):
            return '', False
        return content[:_MAX_CONTENT_LENGTH], True
    except Exception:
        return '', False


def _scrape_url_simple(url: str) -> tuple[str, bool]:
    """Scrape a URL. Direct HTTP first (~1-3s), MCP fallback (~3-8s).
    Returns (content, success).
    """
    # Path 1 — Direct HTTP: wins for public static HTML (most payer provider pages)
    content, ok = _scrape_direct(url)
    if ok:
        return content, True
    # Path 2 — MCP: wins for JS-rendered pages, PDFs, blocked user agents
    content, ok = _scrape_via_mcp(url)
    if ok:
        return content, True
    return '', False


def score_and_scrape_top_result(
    sources: list[dict],
    org_name: str | None = None,
    state: str | None = None,
    max_attempts: int = 3,
    emitter=None,
) -> tuple[str | None, str | None, bool]:
    """Score source URLs and scrape the best one.

    sources:     list of dicts with 'url' or 'link' key (from _run_google_search raw mode)
    org_name:    payer/org name for domain scoring (e.g. 'Sunshine Health')
    state:       state abbreviation for gov domain scoring (e.g. 'FL')
    max_attempts: max URLs to try
    emitter:     thinking emit callback

    Returns (content, source_url, success). content is None if all attempts fail.
    """
    scored: list[tuple[float, str, dict]] = []
    for s in (sources or []):
        url = s.get('url') or s.get('link') or s.get('href') or ''
        if not url or not url.startswith('http'):
            continue
        score = _score_url(url, org_name, state)
        if score > -1.0:
            scored.append((score, url, s))

    if not scored:
        return None, None, False

    scored.sort(key=lambda x: x[0], reverse=True)

    for i, (score, url, _) in enumerate(scored[:max_attempts]):
        # Stop if remaining URLs have no positive relevance signal
        if score <= 0.0 and i > 0:
            break
        # Emit progress before attempting scrape
        if emitter:
            try:
                domain = urllib.parse.urlparse(url).netloc
                emitter(f'◌ Reading page: {domain}')
            except Exception:
                pass
        try:
            content, ok = _scrape_url_simple(url)
            if ok and content:
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
    reconciliation_upload_id: str | None = None,
    reconciliation_org_id: str | None = None,
    thread_id: str | None = None,
    credentialing_options: dict | None = None,
    skill_search_mode: str | None = None,
    pipeline_ctx: Any | None = None,
    tool_inputs: dict[str, Any] | None = None,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Handle tool-path questions via MCP. Returns (answer_text, sources, llm_usage, retrieval_signal).

    tool_inputs: structured inputs for tools invoked via ReAct (e.g. find_org_locations: org_npis, org_npi).
    tool_hint_override: from planner blueprint — bypasses keyword matching.
    scrape_url: explicit URL for web_scrape (detected in resolve.py).
    question_intent: planner question_intent — used as qualifier in search query construction.
    active_context: active jurisdiction state — passed as qualifier ONLY to build_search_query(),
                    never used as a tool search target.
    reconciliation_upload_id, reconciliation_org_id: for run_roster_reconciliation_report.
    thread_id: current chat thread (for list_thread_document_uploads legacy path).
    credentialing_options: from POST /chat envelope (force_refresh, org_name, mode) for roster_report.
    pipeline_ctx: when set, tools may append server-authored choice groups to ctx.pending_workflow_selection
        (merged into response clarification_options in integrate).
    """
    try:
        return _answer_tool_impl(
            question, emitter, invoke_google_for_search_request,
            user_message=user_message, extra_out=extra_out,
            tool_hint_override=tool_hint_override, scrape_url=scrape_url,
            question_intent=question_intent, active_context=active_context,
            reconciliation_upload_id=reconciliation_upload_id,
            reconciliation_org_id=reconciliation_org_id,
            thread_id=thread_id,
            credentialing_options=credentialing_options,
            skill_search_mode=skill_search_mode,
            pipeline_ctx=pipeline_ctx,
            tool_inputs=tool_inputs,
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
    url: str,
    emitter=None,
    scrape_mode: str | None = None,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Scrape a URL and return (answer, sources, usage, retrieval_signal)."""
    mode = normalize_web_scrape_mode(scrape_mode)
    domain = _extract_domain(url) or url[:40]
    _emit(
        emitter,
        {
            WEB_SCRAPE_MODE_QUICK: f"◌ Reading page: {domain}…",
            WEB_SCRAPE_MODE_MEDIUM: f"◌ Site crawl (medium — depth ≤3, up to 6 pages): {domain}…",
            WEB_SCRAPE_MODE_DETAILED: f"◌ Site crawl (detailed — depth ≤5, up to 50 pages, ≤10 doc downloads): {domain}…",
        }[mode],
    )
    args = web_scrape_review_mcp_arguments(url, include_summary=False, scrape_mode=mode)
    timeout = _WEB_SCRAPE_MCP_TIMEOUT[mode]
    try:
        result_text, success = call_mcp_tool(
            TOOL_WEB_SCRAPE_REVIEW,
            args,
            read_timeout=timeout,
        )
    except Exception as e:
        logger.warning("call_mcp_tool web_scrape failed: %s", e, exc_info=True)
        return (f"I ran into an issue calling the tool. {e}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
    result_text = result_text or ""
    if success and result_text:
        cap = _WEB_SCRAPE_RESULT_CAP.get(mode, 8000)
        preview = (result_text[:cap] + "\n\n[... truncated for context window ...]") if len(result_text) > cap else result_text
        src_preview = preview[: min(2000, len(preview))]
        sources = [{"index": 1, "document_name": domain, "text": src_preview[:300], "source_type": "web", "url": url}]
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
    reconciliation_upload_id: str | None = None,
    reconciliation_org_id: str | None = None,
    thread_id: str | None = None,
    credentialing_options: dict | None = None,
    skill_search_mode: str | None = None,
    pipeline_ctx: Any | None = None,
    tool_inputs: dict[str, Any] | None = None,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Implementation of answer_tool. When user_message is set, roster triggers and org name use user_message.

    Tool Isolation Principle: active_context (jurisdiction) is used ONLY as a query qualifier
    in build_search_query(). It is never the search target. Entity tools extract their target
    exclusively from question text via extract_entity_from_question().
    """
    _org_skill_mode = skill_search_mode if skill_search_mode in ("copilot", "agentic") else "copilot"

    from app.stages.agents.capabilities import get_capability_answer


    # ── Intent-based dispatch (from planner blueprint) ────────────────────
    # tool_hint_override bypasses keyword matching entirely. Uses entity extraction
    # so active jurisdiction NEVER bleeds into tool search targets.
    if tool_hint_override:
        hint = tool_hint_override.lower().strip()

        if hint == "document_upload_skill":
            from app.skills.document_upload import DOCUMENT_UPLOAD_SKILL_MARKDOWN

            return (DOCUMENT_UPLOAD_SKILL_MARKDOWN, [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

        if hint == "list_thread_document_uploads":
            from app.skills.document_upload import format_thread_uploads_markdown

            tid = (thread_id or "").strip()
            return (format_thread_uploads_markdown(tid), [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

        # Extract entity from question text — ALWAYS from question, never from active_context
        entity = extract_entity_from_question(text=(user_message or question or ""))
        active = active_context or {}

        if hint == "web_scrape":
            url = scrape_url
            if not url:
                url = _extract_url(question or "") or _extract_url(user_message or "")
            if url:
                ws_mode: str | None = None
                if isinstance(tool_inputs, dict):
                    ws_mode = tool_inputs.get("scrape_mode") or tool_inputs.get("mode")
                return _run_web_scrape(url, emitter=emitter, scrape_mode=ws_mode)
            hint = "google_search"  # no URL — fall through to search

        if hint == "google_search":
            query = build_search_query(entity, active, intent=question_intent)
            if not query.strip():
                query = (question or "").strip()

            # Emit the search query so the user can see what we're looking for
            if emitter:
                emitter(f'◌ Searching the web for: {query[:70]}')

            # Fetch raw results with full URL list for scraping
            raw_results, snippets, usage, signal = _run_google_search(
                query, emitter=emitter, return_raw_results=True,
            )

            # Auto-scrape: score URLs and read the best page
            org_name = entity.get('org_name') or (active or {}).get('payer') or None
            state = (active or {}).get('jurisdiction') or (active or {}).get('state') or 'FL'

            content, source_url, ok = score_and_scrape_top_result(
                raw_results,
                org_name=org_name,
                state=state,
                max_attempts=3,
                emitter=emitter,
            )

            if ok and content:
                domain = _extract_domain(source_url) or (source_url or "")[:40]
                scraped_sources = [{
                    "url": source_url,
                    "source_type": "web",
                    "document_name": domain,
                    "confidence_label": "process_confident",
                }]
                return (content, scraped_sources, usage, RETRIEVAL_SIGNAL_GOOGLE_ONLY)

            # All scrapes failed — LLM-summarise snippets with disclaimer
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
                    disclaimer = (
                        "\n\n[Note: Full page content could not be retrieved. "
                        "These are search result summaries only — "
                        "verify details directly with the payer.]"
                    )
                    return (
                        answer + disclaimer,
                        [{"document_name": "Web search", "source_type": "external"}],
                        llm_usage,
                        RETRIEVAL_SIGNAL_GOOGLE_ONLY,
                    )
                except Exception as e:
                    logger.warning("LLM summarization of search snippets failed: %s", e)
                    return (
                        snippets + "\n\n[Note: These are search result summaries only — verify directly with the payer.]",
                        [{"document_name": "Web search", "source_type": "external"}],
                        None,
                        RETRIEVAL_SIGNAL_GOOGLE_ONLY,
                    )
            return (snippets or "No relevant information found on the web for this query.", [], usage, RETRIEVAL_SIGNAL_NO_SOURCES)

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
        ws_mode_kw: str | None = None
        if isinstance(tool_inputs, dict):
            ws_mode_kw = tool_inputs.get("scrape_mode") or tool_inputs.get("mode")
        return _run_web_scrape(url, emitter=emitter, scrape_mode=ws_mode_kw)
    if wants_scrape and not url:
        return (
            "I can scrape web pages when you give me a URL. Try: 'Scrape https://example.com' or paste the URL.",
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

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
