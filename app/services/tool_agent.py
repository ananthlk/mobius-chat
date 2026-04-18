"""Tool agent: answers capability questions, invokes tools via MCP.

Uses MCP manager to call skills (google_search, web_scrape_review). As we add
tools to mobius-skills-mcp, they are discovered via list_tools—no code changes.
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.services.doc_assembly import (
    RETRIEVAL_SIGNAL_NO_SOURCES,
    RETRIEVAL_SIGNAL_GOOGLE_ONLY,
    RETRIEVAL_SIGNAL_ROSTER_COMPLETE,
)
from app.communication.workflow_selection import (
    attach_workflow_selection,
    build_npi_org_disambiguation_groups,
    format_npi_org_search_markdown,
    format_npi_org_search_summary_for_disambiguation,
)
from app.services.mcp_manager import call_mcp_tool


# 2026-04-18 disconnect: roster_credentialing_orchestrator was deleted.
# tool_agent.py still has ~400 LOC of credentialing-specific helper
# functions (lookup_org_npi, find_org_locations, etc.) that referenced
# run_orchestrator + _provider_roster_base_url from it. With the ReAct
# tool branches removed in commit 2, those helpers are unreachable —
# no caller in the codebase invokes them — but they're still
# module-level and their imports must resolve.
#
# Stubbed below so the module keeps loading. The helpers will fall
# through to "" base URL → HTTPException 503 if anything ever calls
# them, which is the right behavior for orphaned dead code. A later
# cleanup pass will delete the helpers outright; out of scope for
# this commit which is focused on deleting the services + DB modules.

def _provider_roster_base_url() -> str:
    """Stub — credentialing skill is disconnected as of 2026-04-18."""
    return ""


def run_orchestrator(*args, **kwargs):
    """Stub — credentialing skill is disconnected as of 2026-04-18."""
    raise RuntimeError(
        "run_orchestrator was removed with the credentialing disconnect. "
        "If this raises, a caller survived the 2026-04-18 cleanup and "
        "needs to be removed too."
    )

logger = logging.getLogger(__name__)

# ReAct / reasoning: short line for parsers; full markdown remains the tool ``result`` string.
# When merged into one assistant string, use ``compose_mobius_tool_envelope`` (Summary = internal,
# Detail = user display & download) — see ``app.communication.tool_output_envelope``.
REACT_TOOL_SUMMARY_KEY = "react_tool_summary"


def _react_summary_find_locations_data(data: dict[str, Any], *, billing_npi_count: int) -> str:
    locs = data.get("locations") if isinstance(data, dict) else None
    n = len(locs) if isinstance(locs, list) else 0
    sm = (data.get("search_mode") or "").strip() if isinstance(data, dict) else ""
    smbit = f" mode={sm}" if sm else ""
    return (
        f"**Practice locations (Step 2):** {n} site(s) for **{billing_npi_count}** billing org NPI(s){smbit}. "
        f"Registry + DOGE sources; full addresses and `location_id`s are in the markdown detail."
    )


def _react_summary_associated_providers_data(data: dict[str, Any], *, billing_npi_count: int) -> str:
    if not isinstance(data, dict):
        return "**Providers per site (Step 4):** (no structured counts). See markdown detail."
    loc_detail = data.get("location_details") or {}
    active = data.get("active_roster") or {}
    assoc = data.get("associated_providers") or {}
    loc_ids = list(dict.fromkeys([*list(active.keys()), *list(assoc.keys())]))
    n_loc = len(loc_ids)
    pc = data.get("providers_count")
    cutoff = data.get("active_roster_cutoff")
    rr = (data.get("roster_resolution") or "autopilot").strip().lower()
    mv = (data.get("methodology") or {}).get("methodology_version") if isinstance(data.get("methodology"), dict) else None
    parts = [
        f"**Operational roster (Step 4):** {n_loc} location(s), **{billing_npi_count}** billing org NPI(s); "
        f"resolution **{rr}**",
    ]
    if mv:
        parts.append(f" (methodology {mv})")
    parts.append(".")
    if pc is not None:
        parts.append(f" **{pc}** candidate row(s) across sites.")
    if cutoff is not None and rr == "autopilot":
        parts.append(f" Autopilot active panel: score ≥ **{cutoff}**.")
    elif rr == "copilot":
        parts.append(" Copilot: scores and rationales only; active panel set after human confirm.")
    parts.append(" Not a clinical schedule.")
    return "".join(parts)


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


# Maps step_id → ordinal for UI labels ("Step N: …"). Reconciliation from-bq adds master_roster_wide.
_ROSTER_STEP_OUTPUT_NUM: dict[str, int] = {
    "ensure_benchmarks": 1,
    "identify_org": 2,
    "find_locations": 3,
    "find_associated_providers": 4,
    "master_roster_wide": 5,
    "org_benchmark": 5,
    "find_services_by_location": 6,
    "historic_billing_patterns": 7,
    "historic_billing_by_npi": 7,
    "step_6": 8,
    "step_7": 9,
    "opportunity_sizing": 10,
    "opportunity_sizing_detail": 10,
    "taxonomy_benchmarks": 10,
    "build_report": 11,
    "npi_profile": 12,
}

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


def _clean_org_name_for_credentialing(name: str) -> str:
    """Strip jurisdiction/payer/context phrases from org name so search_org_names matches correctly.

    E.g. 'Aspire Health in the context of sunshine jurisdiction' -> 'Aspire Health'.
    Prevents 0 locations when user mentions jurisdiction in the same sentence.
    """
    s = (name or "").strip()
    # Trailing phrases: ", in the context of ...", " for Sunshine", " (Florida)", " in Florida"
    s = re.sub(r",?\s+in\s+the\s+context\s+of\s+[^.]*$", "", s, flags=re.I).strip()
    s = re.sub(r"\s+for\s+(sunshine|sunshine\s+health|united|molina|aetna|humana|cigna|anthem)(\s+.*)?$", "", s, flags=re.I).strip()
    s = re.sub(r"\s*\(?(florida|fl|texas|tx|medicaid|medicare)\)?\s*$", "", s, flags=re.I).strip()
    s = re.sub(r"\s+in\s+(florida|fl|texas|tx)\s*$", "", s, flags=re.I).strip()
    # Leading: "sunshine jurisdiction " or "for Florida "
    s = re.sub(r"^(sunshine\s+(health\s+)?)?(jurisdiction\s*[,:]?\s*)?", "", s, flags=re.I).strip()
    s = re.sub(r"^for\s+(florida|fl)\s+", "", s, flags=re.I).strip()
    return s.strip()


def _ensure_bq_env_for_daily_load() -> None:
    """If BQ_* are not set, load from mobius-chat/.env and mobius-dbt/.env so the daily load subprocess can run dbt."""
    need = ("BQ_PROJECT", "BQ_LANDING_MEDICAID_DATASET", "BQ_MARTS_MEDICAID_DATASET")
    if all(os.environ.get(k) for k in need):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    this_dir = Path(__file__).resolve().parent
    chat_root = this_dir.parent.parent  # mobius-chat
    dbt_root = chat_root.parent / "mobius-dbt"
    for env_path in (chat_root / ".env", dbt_root / ".env"):
        if env_path.is_file():
            load_dotenv(env_path, override=False)
            if all(os.environ.get(k) for k in need):
                return
    # Fallback: load from .env.example so BQ_* are set when user has them only in example
    for env_path in (chat_root / ".env.example", dbt_root / ".env.example"):
        if env_path.is_file():
            load_dotenv(env_path, override=False)
    return


def _last_reload_date_path() -> Path:
    """Path to file storing last FL Medicaid reload date (YYYY-MM-DD)."""
    this_dir = Path(__file__).resolve().parent
    mobius_chat_root = this_dir.parent.parent
    return mobius_chat_root / ".last_fl_medicaid_reload"


def _get_last_reload_date() -> str | None:
    """Return last reload date as YYYY-MM-DD, or None if never reloaded."""
    p = _last_reload_date_path()
    if not p.is_file():
        return None
    try:
        return p.read_text().strip() or None
    except Exception:
        return None


def _set_last_reload_date() -> None:
    """Record that we ran the FL Medicaid daily load today."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _last_reload_date_path().write_text(today)
    except Exception as e:
        logger.debug("Could not write last reload date: %s", e)


def _should_run_first_of_day_reload() -> bool:
    """True if we haven't reloaded FL Medicaid data today (first report of day)."""
    last = _get_last_reload_date()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return last != today


def _run_fl_medicaid_daily_load(emitter) -> None:
    """Run FL Medicaid daily load (scrape PML/PPL, upload, clean, dbt). Forwards [EMIT] lines to emitter.

    Env: MOBIUS_DBT_DIR — path to mobius-dbt repo (default: sibling ../mobius-dbt from this file).
    BQ_PROJECT, BQ_LANDING_MEDICAID_DATASET, BQ_MARTS_MEDICAID_DATASET — loaded from .env if not set.
    Script: scripts/run_fl_medicaid_daily_load.sh (no --skip-download so scrape runs).
    """
    try:
        _ensure_bq_env_for_daily_load()
        # Resolve mobius-dbt root: env or sibling of mobius-chat
        mobius_dbt_dir = (os.environ.get("MOBIUS_DBT_DIR") or "").strip()
        if not mobius_dbt_dir:
            # This file is mobius-chat/app/services/tool_agent.py -> repo is mobius-chat -> sibling mobius-dbt
            this_dir = Path(__file__).resolve().parent
            mobius_chat_root = this_dir.parent.parent  # app -> mobius-chat
            mobius_dbt_dir = str(mobius_chat_root.parent / "mobius-dbt")
        script_path = Path(mobius_dbt_dir) / "scripts" / "run_fl_medicaid_daily_load.sh"
        if not script_path.is_file():
            _emit(emitter, "Reload skipped (MOBIUS_DBT_DIR not set or run_fl_medicaid_daily_load.sh not found).")
            return
        _emit(emitter, "Running Florida Medicaid data reload (scrape → upload → clean → dbt)…")
        proc = subprocess.Popen(
            ["bash", str(script_path)],
            cwd=mobius_dbt_dir,
            env={**os.environ},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = (line or "").strip()
            if line.startswith("[EMIT]"):
                _emit(emitter, line[6:].strip() or line.strip())
        proc.wait()
        if proc.returncode != 0:
            _emit(emitter, "Reload finished with errors (check logs).")
        else:
            _emit(emitter, "Reload complete; data ready for report.")
        _set_last_reload_date()  # record that we ran, so we don't retry on every report today
    except Exception as e:
        logger.warning("FL Medicaid daily load failed: %s", e, exc_info=True)
        _emit(emitter, f"Reload failed: {e}. Proceeding with existing data.")
        _set_last_reload_date()  # still record so we don't retry repeatedly


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


def _emit_org_name_search_envelope(emitter, envelope: dict[str, Any]) -> None:
    """Forward credentialing /search/org-names progress and metadata to the UI."""
    for line in envelope.get("progress") or []:
        if not isinstance(line, str):
            continue
        s = line.strip()
        if not s:
            continue
        _emit(emitter, s if s[:1] in ("◌", "✓", "→") else f"◌ {s}")

    st = envelope.get("sources_tried") or []
    if isinstance(st, list) and st:
        shown = [str(x) for x in st[:15] if x]
        if shown:
            suffix = "…" if len(st) > 15 else ""
            _emit(emitter, "◌ Web / tool sources used: " + ", ".join(shown) + suffix)

    rc = envelope.get("registry_confidence")
    if isinstance(rc, dict) and rc.get("sufficiently_clean") is not None:
        clean = bool(rc["sufficiently_clean"])
        best = rc.get("best_score")
        tier = rc.get("best_tier")
        msg = (
            "◌ Registry signal: top match is strong and separated from alternatives."
            if clean
            else "◌ Registry signal: ambiguous or close ties — please confirm which legal entity you mean."
        )
        extras: list[str] = []
        if tier:
            extras.append(f"tier {tier}")
        if isinstance(best, (int, float)):
            extras.append(f"top score {float(best):.2f}")
        if extras:
            msg = msg.rstrip(".") + " (" + "; ".join(extras) + ")."
        _emit(emitter, msg)


def _run_npi_lookup_by_name(
    org_name: str,
    emitter=None,
    extract_candidate: str = "",
    skill_search_mode: str | None = None,
    pipeline_ctx: Any | None = None,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Enriched NPI lookup: NPPES/PML via credentialing JSON API when available (UI chips), else MCP."""
    org_name = _clean_org_name_for_search(org_name)
    if not org_name or len(org_name) < 2:
        return (
            _append_credentialing_adhoc_hint(
                "I need an organization name to look up NPIs. Try: 'What is the NPI for [org name]?'"
            ),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    sm = skill_search_mode if skill_search_mode in ("copilot", "agentic") else "copilot"
    base = _provider_roster_base_url()
    if base:
        _emit(
            emitter,
            f"◌ Querying credentialing org-name API (NPPES + Florida Medicaid PML, organization NPIs only), search_mode={sm} — «{org_name}»…",
        )
    else:
        _emit(
            emitter,
            "◌ Credentialing service URL not configured — using MCP org_npi_lookup only (no PML merge from this host)…",
        )

    envelope = _fetch_org_search_full(org_name, skill_search_mode=sm, limit=25)
    http_st = envelope.get("http_status")
    if base and http_st is not None and http_st != 200:
        _emit(emitter, f"◌ Credentialing org-name API returned HTTP {http_st} — will try MCP fallback.")
    elif base and envelope.get("api_error") and not envelope.get("results"):
        _emit(emitter, "◌ Credentialing org-name request failed — will try MCP fallback.")

    _emit_org_name_search_envelope(emitter, envelope)

    results = envelope.get("results") or []
    if results:
        exact_n = sum(1 for r in results if (r.get("match_type") or "") == "exact")
        partial_n = sum(1 for r in results if (r.get("match_type") or "") == "partial")
        fuzzy_n = sum(1 for r in results if (r.get("match_type") or "") in ("fuzzy", "none"))
        _emit(
            emitter,
            f"◌ Credentialing API: {len(results)} candidate row(s) after merge — exact {exact_n}, partial {partial_n}, other {fuzzy_n}. Formatting answer…",
        )
        groups: list[dict[str, Any]] = []
        if pipeline_ctx is not None and len(results) > 1:
            groups = build_npi_org_disambiguation_groups(results, org_name) or []
            attach_workflow_selection(pipeline_ctx, groups)
        if groups:
            body = format_npi_org_search_summary_for_disambiguation(org_name, results)
        else:
            body = format_npi_org_search_markdown(org_name, results)
        body = _append_credentialing_adhoc_hint(body)
        sources = [{"index": 1, "document_name": "NPPES / PML (enriched)", "text": body[:300], "source_type": "external"}]
        return (body, sources, None, RETRIEVAL_SIGNAL_NO_SOURCES)

    if base and http_st == 200:
        _emit(emitter, f"◌ No organization candidates from credentialing API for «{org_name}» — trying MCP org_npi_lookup…")

    try:
        _emit(emitter, "◌ MCP: calling org_npi_lookup…")
        result_text, success = call_mcp_tool(
            TOOL_ORG_NPI_LOOKUP,
            {"name": org_name, "state": "FL", "limit": 10, "search_mode": sm},
        )
    except Exception as e:
        logger.warning("call_mcp_tool org_npi_lookup failed: %s", e, exc_info=True)
        return (
            _append_credentialing_adhoc_hint(f"I ran into an issue looking up NPIs. {e}. Please try again."),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    if success and result_text and "Error:" not in result_text:
        out = _append_credentialing_adhoc_hint(result_text)
        sources = [{"index": 1, "document_name": "NPPES / PML (enriched)", "text": out[:300], "source_type": "external"}]
        return (out, sources, None, RETRIEVAL_SIGNAL_NO_SOURCES)

    tail = result_text if result_text else f"No NPIs found for '{org_name}'. Try the exact legal name or an address."
    return (_append_credentialing_adhoc_hint(tail), [], None, RETRIEVAL_SIGNAL_NO_SOURCES)


def _normalize_org_npi_digits(raw: str) -> str | None:
    if not raw or not str(raw).strip():
        return None
    d = re.sub(r"\D", "", str(raw).strip())
    if not d:
        return None
    if len(d) < 10:
        d = d.zfill(10)
    if len(d) != 10:
        return None
    return d


def _collect_npis_from_texts(*texts: str) -> list[str]:
    seen: list[str] = []
    pat = re.compile(r"\b(\d{10})\b")
    for t in texts:
        if not t:
            continue
        for m in pat.findall(t):
            n = _normalize_org_npi_digits(m)
            if n and n not in seen:
                seen.append(n)
    return seen


def _resolve_org_npis_for_find_locations(
    org_name: str,
    org_npi: str,
    org_npis_in: list[Any] | None,
    user_message: str,
    prior_lookup_text: str,
    skill_search_mode: str,
    emitter,
) -> tuple[list[str], str, str, list[dict[str, Any]] | None]:
    """Returns (billing org npi list, org_name hint, error markdown or "", disambiguation rows or None).

    When the name matches several billing org NPIs, ``disambiguation_rows`` is the candidate list
    for the same ``clarification_options`` / workflow selection UX as ``lookup_npi``.
    """
    out: list[str] = []
    if org_npis_in:
        for x in org_npis_in:
            n = _normalize_org_npi_digits(str(x))
            if n:
                out.append(n)
        out = list(dict.fromkeys(out))
        if out:
            return out, (org_name or "").strip(), "", None
    n1 = _normalize_org_npi_digits(org_npi)
    if n1:
        return [n1], (org_name or "").strip(), "", None
    from_text = _collect_npis_from_texts(user_message or "", prior_lookup_text or "", org_name or "")
    if from_text:
        return from_text, (org_name or "").strip(), "", None
    name = (org_name or "").strip()
    if not name:
        return (
            [],
            "",
            "I need at least one **billing organization NPI** (10 digits) or an **organization name** that resolves "
            "to a single NPI. You can paste NPI(s) from the list above or say e.g. "
            "**Find practice locations for NPI 1234567893**.",
            None,
        )
    envelope = _fetch_org_search_full(name, skill_search_mode=skill_search_mode, limit=25)
    results = envelope.get("results") or []
    exact = [r for r in results if (r.get("match_type") or "") == "exact"]
    if len(exact) == 1:
        n = _normalize_org_npi_digits(str(exact[0].get("npi") or ""))
        if n:
            return [n], name, "", None
    if len(exact) > 1:
        body = format_npi_org_search_markdown(name, exact[:20])
        return (
            [],
            "",
            f"Several billing organizations match «{name}» exactly. Pick one **NPI** and ask again "
            f"(e.g. *Find practice locations for NPI …*).\n\n{body}",
            exact[:20],
        )
    if len(results) == 1:
        n = _normalize_org_npi_digits(str(results[0].get("npi") or ""))
        if n:
            return [n], name, "", None
    if not results:
        return (
            [],
            "",
            f"No NPPES/PML organization candidates for «{name}». Try `lookup_npi` first or paste a 10-digit billing NPI.",
            None,
        )
    body = format_npi_org_search_markdown(name, results[:18])
    return (
        [],
        "",
        f"Could not pick a single billing NPI for «{name}». Narrow the name or paste an NPI.\n\n{body}",
        results[:18],
    )


# Ad-hoc tools mirror credentialing API steps but do not run the orchestrated workflow (no persisted assertions).
_CREDENTIALING_ADHOC_WORKFLOW_HINT = (
    "\n\n---\n\n"
    "_**Ad-hoc lookup:** Same data sources as the credentialing pipeline, but this is **not** the orchestrated "
    "workflow—nothing here is written to **credentialing assertions** or the **roster review** session. "
    "To validate and persist org NPIs, locations, and providers step by step, run a **credentialing report in "
    "co-pilot** mode; for a full pass without per-step review, use **autopilot**. To persist **upload vs "
    "external** roster comparison, use **roster reconciliation**._"
)


def _append_credentialing_adhoc_hint(text: str) -> str:
    """Append workflow-integrity footnote for any standalone credentialing-skill response."""
    body = (text or "").rstrip()
    if not body:
        return _CREDENTIALING_ADHOC_WORKFLOW_HINT.strip()
    return f"{body}{_CREDENTIALING_ADHOC_WORKFLOW_HINT}"


def _format_find_locations_markdown_chat(data: dict[str, Any]) -> str:
    locs = data.get("locations") or []
    lines: list[str] = [f"# Practice locations ({len(locs)} site(s))", ""]
    mode = (data.get("search_mode") or "").strip()
    if mode:
        lines.append(f"**search_mode:** {mode}")
        lines.append("")
    prog = data.get("progress") or []
    if prog:
        lines.append("## Progress")
        for p in prog[:40]:
            lines.append(f"- {p}")
        lines.append("")
    stried = data.get("sources_tried") or []
    if stried:
        lines.append("**Sources tried:** " + ", ".join(str(x) for x in stried[:20]))
        lines.append("")
    if not locs:
        lines.append("_No locations returned._")
    else:
        lines.append("## Sites")
        lines.append("")
        for i, loc in enumerate(locs, 1):
            if not isinstance(loc, dict):
                continue
            a1 = loc.get("site_address_line_1") or ""
            city = loc.get("site_city") or ""
            st = loc.get("site_state") or ""
            z = loc.get("site_zip5") or loc.get("site_zip") or ""
            lines.append(f"{i}. **{a1}**, {city}, {st} {z}".strip())
            lid = loc.get("location_id") or ""
            src = loc.get("site_source") or ""
            lname = loc.get("name")
            if lname:
                lines.append(f"   - Name: {lname}")
            lines.append(f"   - `location_id`: `{lid}` · `site_source`: {src}")
            lines.append("")
    pml = data.get("org_npis_pml_status")
    if pml:
        lines.append("## Org NPI Medicaid (PML) status snapshot")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(pml, default=str, indent=2)[:8000])
        lines.append("```")
    body = "\n".join(lines).strip()
    return _append_credentialing_adhoc_hint(body)


def _roster_step_output_find_locations(data: dict[str, Any], *, row_count: int) -> list[dict[str, Any]]:
    """Structured step for chat UI: full practice-location list (matches roster_step_outputs contract)."""
    md = _format_find_locations_markdown_chat(data)
    return [
        {
            "step_id": "find_locations",
            "step_num": _ROSTER_STEP_OUTPUT_NUM.get("find_locations", 3),
            "label": "Practice locations — full list",
            "csv_content": "",
            "row_count": row_count,
            "markdown_content": md,
        }
    ]


def _run_find_org_locations_for_chat(
    tool_inputs: dict[str, Any] | None,
    *,
    question: str,
    user_message: str,
    active_context: dict[str, Any] | None,
    skill_search_mode: str,
    emitter=None,
    pipeline_ctx: Any | None = None,
    extra_out: dict[str, Any] | None = None,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """POST /find-locations (Step 2) — same backend as MCP ``find_org_locations``."""
    ins = dict(tool_inputs or {})
    org_name = str(ins.get("org_name") or "").strip()
    org_npi = str(ins.get("org_npi") or "").strip()
    raw_list = ins.get("org_npis")
    org_npis_in = raw_list if isinstance(raw_list, list) else None
    state = str(ins.get("state") or "FL").strip().upper() or "FL"
    include_web = bool(ins.get("include_web_enrichment", False))

    prior = ""
    if isinstance(active_context, dict) and (active_context.get("tool") == "lookup_npi"):
        prior = (
            str(active_context.get("full_output") or "")
            or str(active_context.get("summary") or "")
        )[:12000]

    org_npis, name_hint, err, disambig_rows = _resolve_org_npis_for_find_locations(
        org_name,
        org_npi,
        org_npis_in,
        user_message or "",
        prior,
        skill_search_mode,
        emitter,
    )
    if err:
        label = (name_hint or org_name or "").strip()
        if (
            pipeline_ctx is not None
            and disambig_rows
            and len(disambig_rows) > 1
            and label
        ):
            groups = build_npi_org_disambiguation_groups(disambig_rows, label) or []
            if groups:
                attach_workflow_selection(pipeline_ctx, groups)
                brief = format_npi_org_search_summary_for_disambiguation(label, disambig_rows)
                pipeline_ctx.active_context = {
                    "tool": "lookup_npi",
                    "org": label,
                    "summary": brief[:300],
                    "full_output": err,
                    "follow_up_capable": True,
                    "expires_after_turns": 8,
                }
                return (_append_credentialing_adhoc_hint(brief), [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
        return (_append_credentialing_adhoc_hint(err), [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

    base = _provider_roster_base_url()
    if not base:
        return (
            _append_credentialing_adhoc_hint(
                "Practice location lookup requires the credentialing API. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL."
            ),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    _emit(
        emitter,
        f"◌ Finding practice locations for {len(org_npis)} billing NPI(s) "
        f"(POST /find-locations, search_mode={skill_search_mode})…",
    )
    url = f"{base.rstrip('/')}/find-locations"
    payload: dict[str, Any] = {
        "org_npis": org_npis,
        "state": state,
        "search_mode": skill_search_mode,
        "include_web_enrichment": include_web,
    }
    if name_hint:
        payload["org_name"] = name_hint
    timeout_sec = 120.0 if skill_search_mode == "agentic" else 75.0
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        body = (e.response.text or "")[:800]
        logger.warning("find_org_locations HTTP %s %s", e.response.status_code, body)
        return (
            _append_credentialing_adhoc_hint(
                f"Practice location lookup failed ({e.response.status_code}): {body or str(e)}"
            ),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )
    except Exception as e:
        logger.warning("find_org_locations failed: %s", e, exc_info=True)
        return (
            _append_credentialing_adhoc_hint(
                f"Practice location lookup failed: {e}. Ensure provider-roster-credentialing is running."
            ),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    if isinstance(data, dict):
        for line in (data.get("progress") or [])[:35]:
            s = str(line).strip()
            if s:
                _emit(emitter, s if s.startswith("◌") else f"◌ {s}")
        tried = data.get("sources_tried") or []
        if isinstance(tried, list) and tried:
            _emit(emitter, "◌ Sources tried: " + ", ".join(str(x) for x in tried[:15]))

    text = _format_find_locations_markdown_chat(data if isinstance(data, dict) else {})
    locs = data.get("locations") if isinstance(data, dict) else None
    n_locs = len(locs) if isinstance(locs, list) else 0
    if extra_out is not None and isinstance(data, dict) and n_locs > 0:
        extra_out["roster_step_outputs"] = _roster_step_output_find_locations(data, row_count=n_locs)
    sources = [
        {
            "index": 1,
            "document_name": "Practice locations (credentialing API)",
            "text": text[:400],
            "source_type": "external",
        }
    ]
    sig = RETRIEVAL_SIGNAL_GOOGLE_ONLY if skill_search_mode == "agentic" else RETRIEVAL_SIGNAL_NO_SOURCES
    if isinstance(extra_out, dict) and isinstance(data, dict):
        extra_out[REACT_TOOL_SUMMARY_KEY] = _react_summary_find_locations_data(
            data, billing_npi_count=len(org_npis)
        )
    return (text, sources, None, sig)


def _format_associated_providers_markdown_chat(data: dict[str, Any]) -> str:
    """Step 4 output: providers implicated per site (operational roster, not clinical staffing)."""
    meth = data.get("methodology") if isinstance(data.get("methodology"), dict) else {}
    summary_m = (meth.get("methodology_summary") or "").strip()
    rr = (data.get("roster_resolution") or "autopilot").strip().lower()
    lines: list[str] = [
        "# Providers implicated at each practice site",
        "",
        "_**Operational roster** (credentialing Step 4): Billing / enrollment alignment only — "
        "**not** who is clinically staffing a site today._",
        "",
        f"**Resolution mode:** **{rr}** — "
        + (
            "autopilot applies score cutoff to label an **active panel**."
            if rr == "autopilot"
            else "copilot returns **evidence and scores**; **active** labels wait for human confirmation."
        ),
        "",
    ]
    if summary_m:
        lines.append(f"**How this list was built:** {summary_m}")
        lines.append("")
    glossary = meth.get("methodology_glossary") if isinstance(meth.get("methodology_glossary"), dict) else {}
    if glossary:
        lines.append("<details><summary>Match basis (plain language)</summary>")
        lines.append("")
        for code, entry in list(glossary.items())[:12]:
            if not isinstance(entry, dict):
                continue
            lab = entry.get("user_label") or code
            desc = (entry.get("user_description") or "").strip()
            lines.append(f"- **{lab}** (`{code}`): {desc}")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    cutoff = data.get("active_roster_cutoff")
    if cutoff is not None and rr == "autopilot":
        lines.append(
            f"**Autopilot active panel:** score ≥ **{cutoff}/100** after registry penalties "
            f"(`roster_status=active` vs `historic`)."
        )
        lines.append("")
    elif rr == "copilot":
        lines.append(
            "**Copilot:** each row shows **score /100** and **status `pending_review`** until you confirm an active panel."
        )
        lines.append("")
    loc_detail = data.get("location_details") or {}
    active = data.get("active_roster") or {}
    assoc = data.get("associated_providers") or {}
    if not active and not assoc:
        lines.append("_No providers returned._")
        return _append_credentialing_adhoc_hint("\n".join(lines).strip())
    loc_ids = list(dict.fromkeys([*list(active.keys()), *list(assoc.keys())]))
    active_total = sum(len(active.get(lid) or []) for lid in loc_ids)
    assoc_total = sum(len(assoc.get(lid) or []) for lid in loc_ids)
    for lid in loc_ids:
        det = loc_detail.get(lid) or {}
        addr = det.get("location_address") or str(lid)
        alist = active.get(lid) or []
        plist = assoc.get(lid) or []
        lines.append(f"## {addr}")
        lines.append("")
        lines.append(f"`location_id`: `{lid}`")
        lines.append("")
        if rr == "autopilot":
            lines.append(
                f"**At this site:** **{len(alist)}** in the autopilot **active** panel "
                f"of **{len(plist)}** candidate row(s)."
            )
        else:
            lines.append(f"**At this site:** **{len(plist)}** candidate row(s) (active panel **not** applied).")
        lines.append("")
        show = plist
        for j, p in enumerate(show[:50], 1):
            if not isinstance(p, dict):
                continue
            npi = p.get("npi", "")
            name = p.get("name", "")
            score = p.get("association_likelihood", "")
            basis = (p.get("basis_user") or "").strip() or str(p.get("match_type", "") or "—")
            rs = p.get("roster_status", "")
            rs_disp = {"active": "active panel", "historic": "below cutoff", "pending_review": "pending review"}.get(
                str(rs), str(rs) or "—"
            )
            lines.append(
                f"{j}. **NPI {npi}** — {name} — **Score {score}/100** — _{rs_disp}_ — basis: {basis}"
            )
        if len(show) > 50:
            lines.append(f"... _{len(show) - 50} more at this site_")
        lines.append("")
    pc = data.get("providers_count")
    if pc is not None:
        if rr == "autopilot":
            lines.append(
                f"**Across all listed sites:** **{assoc_total}** candidate row(s); **{active_total}** in autopilot active panel."
            )
        else:
            lines.append(f"**Across all listed sites:** **{assoc_total}** candidate row(s) ({pc} reported by API).")
    return _append_credentialing_adhoc_hint("\n".join(lines).strip())


def _run_find_associated_providers_for_chat(
    tool_inputs: dict[str, Any] | None,
    *,
    question: str,
    user_message: str,
    active_context: dict[str, Any] | None,
    skill_search_mode: str,
    emitter=None,
    pipeline_ctx: Any | None = None,
    extra_out: dict[str, Any] | None = None,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """POST /find-locations then /find-associated-providers — same backend as MCP ``find_associated_providers_at_locations``."""
    ins = dict(tool_inputs or {})
    org_name = str(ins.get("org_name") or "").strip()
    org_npi = str(ins.get("org_npi") or "").strip()
    raw_list = ins.get("org_npis")
    org_npis_in = raw_list if isinstance(raw_list, list) else None
    state = str(ins.get("state") or "FL").strip().upper() or "FL"
    include_web = bool(ins.get("include_web_enrichment", False))
    upload_id = str(ins.get("upload_id") or "").strip()
    if not upload_id and isinstance(active_context, dict):
        upload_id = (active_context.get("reconciliation_upload_id") or "").strip()
    include_roster = ins.get("include_roster_members")
    if include_roster is None:
        include_roster = True
    external_only = bool(ins.get("external_only", False))

    prior = ""
    if isinstance(active_context, dict) and (active_context.get("tool") == "lookup_npi"):
        prior = (
            str(active_context.get("full_output") or "")
            or str(active_context.get("summary") or "")
        )[:12000]

    org_npis, name_hint, err, disambig_rows = _resolve_org_npis_for_find_locations(
        org_name,
        org_npi,
        org_npis_in,
        user_message or "",
        prior,
        skill_search_mode,
        emitter,
    )
    if err:
        label = (name_hint or org_name or "").strip()
        if (
            pipeline_ctx is not None
            and disambig_rows
            and len(disambig_rows) > 1
            and label
        ):
            groups = build_npi_org_disambiguation_groups(disambig_rows, label) or []
            if groups:
                attach_workflow_selection(pipeline_ctx, groups)
                brief = format_npi_org_search_summary_for_disambiguation(label, disambig_rows)
                pipeline_ctx.active_context = {
                    "tool": "lookup_npi",
                    "org": label,
                    "summary": brief[:300],
                    "full_output": err,
                    "follow_up_capable": True,
                    "expires_after_turns": 8,
                }
                return (_append_credentialing_adhoc_hint(brief), [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
        return (_append_credentialing_adhoc_hint(err), [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

    base = _provider_roster_base_url()
    if not base:
        return (
            _append_credentialing_adhoc_hint(
                "Provider–location lookup requires the credentialing API. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL."
            ),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    _emit(
        emitter,
        f"◌ Finding practice locations, then providers per site (POST /find-locations + /find-associated-providers)…",
    )
    url = f"{base.rstrip('/')}/find-locations"
    payload: dict[str, Any] = {
        "org_npis": org_npis,
        "state": state,
        "search_mode": skill_search_mode,
        "include_web_enrichment": include_web,
    }
    if name_hint:
        payload["org_name"] = name_hint
    timeout_sec = 120.0 if skill_search_mode == "agentic" else 75.0
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            loc_data = resp.json()
    except httpx.HTTPStatusError as e:
        body = (e.response.text or "")[:800]
        logger.warning("find_associated (find-locations) HTTP %s %s", e.response.status_code, body)
        return (
            _append_credentialing_adhoc_hint(
                f"Practice location lookup failed ({e.response.status_code}): {body or str(e)}"
            ),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )
    except Exception as e:
        logger.warning("find_associated find-locations failed: %s", e, exc_info=True)
        return (
            _append_credentialing_adhoc_hint(
                f"Practice location lookup failed: {e}. Ensure provider-roster-credentialing is running."
            ),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    locations = loc_data.get("locations") if isinstance(loc_data, dict) else None
    if not locations:
        return (
            _append_credentialing_adhoc_hint(
                "No practice locations returned — cannot list providers per site."
            ),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    if isinstance(loc_data, dict):
        for line in (loc_data.get("progress") or [])[:20]:
            s = str(line).strip()
            if s:
                _emit(emitter, s if s.startswith("◌") else f"◌ {s}")

    assoc_url = f"{base.rstrip('/')}/find-associated-providers"
    res_mode = "autopilot" if skill_search_mode == "agentic" else "copilot"
    assoc_payload: dict[str, Any] = {
        "org_npis": org_npis,
        "locations": locations,
        "org_name": name_hint or org_name,
        "include_roster_members": bool(include_roster),
        "external_only": external_only,
        "roster_resolution": res_mode,
    }
    if upload_id:
        assoc_payload["upload_id"] = upload_id
    try:
        with httpx.Client(timeout=180.0) as client:
            aresp = client.post(assoc_url, json=assoc_payload)
            aresp.raise_for_status()
            ap_data = aresp.json()
    except httpx.HTTPStatusError as e:
        body = (e.response.text or "")[:800]
        logger.warning("find_associated HTTP %s %s", e.response.status_code, body)
        return (
            _append_credentialing_adhoc_hint(
                f"Find-associated-providers failed ({e.response.status_code}): {body or str(e)}"
            ),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )
    except Exception as e:
        logger.warning("find_associated failed: %s", e, exc_info=True)
        return (
            _append_credentialing_adhoc_hint(f"Find-associated-providers failed: {e}."),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )

    text = _format_associated_providers_markdown_chat(ap_data if isinstance(ap_data, dict) else {})
    sources = [
        {
            "index": 1,
            "document_name": "Providers per site (credentialing API)",
            "text": text[:400],
            "source_type": "external",
        }
    ]
    sig = RETRIEVAL_SIGNAL_GOOGLE_ONLY if skill_search_mode == "agentic" else RETRIEVAL_SIGNAL_NO_SOURCES
    if isinstance(extra_out, dict) and isinstance(ap_data, dict):
        extra_out[REACT_TOOL_SUMMARY_KEY] = _react_summary_associated_providers_data(
            ap_data, billing_npi_count=len(org_npis)
        )
    return (text, sources, None, sig)


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
        return (
            _append_credentialing_adhoc_hint(f"I ran into an issue with the address lookup. {e}. Please try again."),
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )
    if success and result_text and "Error:" not in result_text and "No matches found" not in result_text:
        out = _append_credentialing_adhoc_hint(result_text)
        sources = [{"index": 1, "document_name": "Address lookup", "text": out[:300], "source_type": "external"}]
        return (out, sources, None, RETRIEVAL_SIGNAL_NO_SOURCES)
    tail = result_text if result_text else f"No providers found at '{address}'. Try a more specific address."
    return (_append_credentialing_adhoc_hint(tail), [], None, RETRIEVAL_SIGNAL_NO_SOURCES)


def _serve_cached_credentialing_report(
    run: dict[str, Any], org_name: str, extra_out: dict[str, Any] | None
) -> tuple[str, list[dict], None, str]:
    """Return cached report in same format as orchestrator output."""
    step_order = dict(_ROSTER_STEP_OUTPUT_NUM)
    steps = run.get("step_outputs") or []
    if extra_out is not None:
        extra_out["roster_step_outputs"] = [
            {
                "step_id": s.get("step_id", ""),
                "step_num": step_order.get(s.get("step_id", ""), 0),
                "label": s.get("label", ""),
                "csv_content": s.get("content", "") if (s.get("content_type") or "") == "csv" else "",
                "row_count": s.get("row_count", 0) or 0,
                "markdown_content": s.get("content", "") if "markdown" in (s.get("content_type") or "") else "",
                "json_content": s.get("content", "") if (s.get("content_type") or "") == "json" else "",
            }
            for s in steps
        ]
        extra_out["report_run_id"] = (run.get("report_run_id") or "").strip()
        extra_out["last_report_org"] = org_name
        docs = run.get("documents") or {}
        extra_out["roster_report_pdf_base64"] = docs.get("final_pdf_base64") or ""
        extra_out["roster_report_final_md"] = docs.get("final_md") or ""
        if (docs.get("final_md") or "").strip() or (docs.get("final_pdf_base64") or "").strip():
            extra_out["roster_report_attachments_kind"] = "credentialing"
    result_text = (run.get("documents") or {}).get("final_md") or "Report loaded from cache."
    sources = [
        {
            "index": 1,
            "document_name": "Provider Roster / Credentialing (cached)",
            "text": (result_text or "")[:300],
            "source_type": "external",
        }
    ]
    return (result_text, sources, None, RETRIEVAL_SIGNAL_ROSTER_COMPLETE)


def _get_latest_run_for_org(org_name: str):
    """GET /report-runs/latest?org_name=... Return run dict or None."""
    base = _provider_roster_base_url()
    if not base or not (org_name or "").strip():
        return None
    url = f"{base.rstrip('/')}/report-runs/latest"
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, params={"org_name": (org_name or "").strip()})
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.debug("get_latest_run_for_org failed: %s", e)
        return None


def _fetch_org_search_full(
    org_name: str,
    *,
    skill_search_mode: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """POST /search/org-names; return results plus progress/sources_tried/registry_confidence from the skill."""
    sm = skill_search_mode if skill_search_mode in ("copilot", "agentic") else "copilot"
    empty: dict[str, Any] = {
        "results": [],
        "progress": [],
        "sources_tried": [],
        "registry_confidence": None,
        "search_mode": sm,
        "http_status": None,
        "api_error": None,
    }
    base = _provider_roster_base_url()
    if not base or not (org_name or "").strip():
        return empty
    url = f"{base.rstrip('/')}/search/org-names"
    payload = {
        "name": (org_name or "").strip(),
        "state": "FL",
        "limit": min(50, max(1, limit)),
        "include_pml": True,
        "entity_type_filter": "2",
        "search_mode": sm,
    }
    read_timeout = 75.0 if sm == "agentic" else 35.0
    try:
        with httpx.Client(timeout=read_timeout) as client:
            resp = client.post(url, json=payload)
            empty["http_status"] = resp.status_code
            if resp.status_code != 200:
                empty["api_error"] = (resp.text or "")[:500]
                return empty
            data = resp.json()
    except Exception as e:
        logger.debug("fetch org search full failed: %s", e)
        empty["api_error"] = str(e)
        return empty
    raw = data.get("results") or []
    empty["results"] = raw if isinstance(raw, list) else []
    prog = data.get("progress")
    empty["progress"] = prog if isinstance(prog, list) else []
    st = data.get("sources_tried")
    empty["sources_tried"] = st if isinstance(st, list) else []
    empty["registry_confidence"] = data.get("registry_confidence")
    sm_out = data.get("search_mode")
    if isinstance(sm_out, str) and sm_out.strip():
        empty["search_mode"] = sm_out.strip()
    return empty


def _fetch_org_search_results_full(
    org_name: str,
    *,
    skill_search_mode: str | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """POST /search/org-names; return raw ``results`` list only (same contract as MCP org search)."""
    return _fetch_org_search_full(org_name, skill_search_mode=skill_search_mode, limit=limit)["results"]


def _get_org_name_candidates(
    org_name: str,
    limit: int = 10,
    *,
    skill_search_mode: str | None = None,
) -> list[str]:
    """Call credentialing skill POST /search/org-names; return unique org names for plan-B matching.
    Used when direct latest-run lookup fails (e.g. 'David Lawrence' vs stored 'David Lawrence Center')."""
    base = _provider_roster_base_url()
    if not base or not (org_name or "").strip():
        return []
    url = f"{base.rstrip('/')}/search/org-names"
    sm = skill_search_mode if skill_search_mode in ("copilot", "agentic") else "copilot"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                url,
                json={
                    "name": (org_name or "").strip(),
                    "state": "FL",
                    "limit": 20,
                    "search_mode": sm,
                },
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception as e:
        logger.debug("get_org_name_candidates failed: %s", e)
        return []
    results = data.get("results") or []
    seen: set[str] = set()
    out: list[str] = []
    for r in results:
        name = (r.get("name") or "").strip()
        if name and len(name) >= 2 and name not in seen:
            seen.add(name)
            out.append(name)
            if len(out) >= limit:
                break
    return out


def _is_plausible_org_name(org: str) -> bool:
    """False if the string looks like a sentence or follow-up, not an organization name."""
    s = (org or "").strip()
    if not s or len(s) < 2:
        return False
    if len(s) > 55:
        return False
    lower = s.lower()
    # Follow-up phrases that must not be treated as org name
    if any(
        x in lower
        for x in (
            "section a",
            "section b",
            "section c",
            "section d",
            "section e",
            "explain section",
            "i meant",
            "of the credentialing report",
            "of the report",
            "what does the report",
            "how many npi",
            "how many providers",
        )
    ):
        return False
    return True


# Generic NPI/credentialing answer when no report in context (credentialing_qa path)
# Flow: thread → persisted → create? → don't have. Suggests NPPES fallback for NPI lookup.
CREDENTIALING_QA_NO_REPORT = (
    "I don't have a credentialing report in this conversation. "
    "Say **Create a credentialing report for [organization name]** to generate one. "
    "For basic NPI info (name, taxonomy, address) without PML status, I can look up from NPPES."
)


def _ask_credentialing_report(
    report_run_id: str,
    question: str,
    emitter=None,
) -> tuple[str, list[dict], dict[str, Any] | None, str]:
    """Call provider-roster-credentialing POST /report-runs/{id}/ask. Returns (answer, sources, usage, signal)."""
    base = _provider_roster_base_url()
    if not base:
        return (
            "Report Q&A is not configured. Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL.",
            [],
            None,
            RETRIEVAL_SIGNAL_NO_SOURCES,
        )
    url = f"{base.rstrip('/')}/report-runs/{report_run_id}/ask"
    _emit(emitter, "Asking the report…")
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, json={"question": (question or "").strip()})
            resp.raise_for_status()
            data = resp.json()
            answer = (data.get("answer") or "").strip()
            if not answer:
                answer = "No answer returned from the report."
            sources = [{"index": 1, "document_name": "Credentialing report", "text": answer[:300], "source_type": "external"}]
            return (answer, sources, None, RETRIEVAL_SIGNAL_ROSTER_COMPLETE)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return ("That report run was not found. It may have expired or been created without persistence.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
        if e.response.status_code == 503:
            return ("Report persistence is disabled on the credentialing service, so report Q&A is unavailable.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
        logger.warning("Report ask failed: %s", e, exc_info=True)
        return (f"I couldn't ask the report: {e!s}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
    except Exception as e:
        logger.warning("Report ask failed: %s", e, exc_info=True)
        return (f"I ran into an issue asking the report. {e!s}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)


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

    # Alias: ask_credentialing_npi (ReAct tool name) → credentialing_qa (internal hint)
    if tool_hint_override and (tool_hint_override or "").strip().lower() == "ask_credentialing_npi":
        tool_hint_override = "credentialing_qa"

    if tool_hint_override and (tool_hint_override or "").strip().lower() == "find_org_locations":
        return _run_find_org_locations_for_chat(
            tool_inputs,
            question=(question or ""),
            user_message=(user_message or question or ""),
            active_context=active_context if isinstance(active_context, dict) else None,
            skill_search_mode=_org_skill_mode,
            emitter=emitter,
            pipeline_ctx=pipeline_ctx,
            extra_out=extra_out,
        )

    if tool_hint_override and (tool_hint_override or "").strip().lower() == "find_associated_providers_at_locations":
        return _run_find_associated_providers_for_chat(
            tool_inputs,
            question=(question or ""),
            user_message=(user_message or question or ""),
            active_context=active_context if isinstance(active_context, dict) else None,
            skill_search_mode=_org_skill_mode,
            emitter=emitter,
            pipeline_ctx=pipeline_ctx,
            extra_out=extra_out if isinstance(extra_out, dict) else None,
        )

    # ── Roster reconciliation: upload vs outside-in ──
    if tool_hint_override and (tool_hint_override or "").strip().lower() == "roster_reconciliation":
        org_name = (question or "").strip()
        upload_id = (reconciliation_upload_id or "").strip()
        org_id = (reconciliation_org_id or "").strip()
        # 2026-04-18 disconnect: roster_source_of_truth removed along with
        # the rest of the credentialing services. This branch is
        # unreachable after the ReAct tool dispatch cleanup (commit 2 of
        # the disconnect), so the downstream code here will raise a
        # clear error if anything reaches it instead of silently missing
        # the upload_id resolution.
        base = _provider_roster_base_url()
        if not base or not org_name or not upload_id or not org_id:
            missing = []
            if not base:
                missing.append("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL")
            if not org_name:
                missing.append("org_name")
            if not upload_id:
                missing.append("upload_id")
            if not org_id:
                missing.append("org_id")
            hint = ""
            if not org_id and org_name:
                hint = (
                    f" org_id is the organization's billing NPI — required to fetch the external roster "
                    "(claims + address propensity). Use search_org_names to find the billing NPI for "
                    f"'{org_name}', or ask the user to provide it."
                )
            elif not upload_id and org_id:
                hint = (
                    " No resolved roster was found in the provider database for this billing NPI "
                    "(source of truth). Upload and process a roster for this org, or verify the NPI."
                )
            return (
                f"Roster reconciliation needs org_name, upload_id, and org_id. Missing: {', '.join(missing)}.{hint}",
                [],
                None,
                RETRIEVAL_SIGNAL_NO_SOURCES,
            )
        try:
            stream_url = f"{base.rstrip('/')}/roster-reconciliation-report/from-bq/stream"
            json_url = f"{base.rstrip('/')}/roster-reconciliation-report/from-bq"
            payload = {"org_name": org_name, "upload_id": upload_id, "org_id": org_id}
            # Long read timeout: report LLM can run 5–15+ minutes; explicit connect/read avoids pool defaults.
            timeout_long = httpx.Timeout(connect=60.0, read=900.0, write=120.0, pool=60.0)
            data: dict[str, Any] | None = None

            def _consume_sse_stream(client: httpx.Client) -> dict[str, Any] | None:
                out: dict[str, Any] | None = None
                with client.stream("POST", stream_url, json=payload) as r:
                    # Streaming responses must have the body read before raise_for_status
                    # (otherwise HTTPStatusError.response.text raises ResponseNotRead).
                    if r.is_error:
                        r.read()
                        r.raise_for_status()
                    for line in r.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        try:
                            import json as _json

                            raw = line[5:].strip()
                            ev = _json.loads(raw)
                            evt = ev.get("event") or ""
                            if evt == "progress" and emitter:
                                msg = (ev.get("message") or "").strip()
                                if msg:
                                    emitter(msg)
                            elif evt == "complete":
                                out = ev.get("result") or {}
                                break
                            elif evt == "error":
                                return {"__stream_error__": str(ev.get("message") or "Stream error")}
                        except Exception:
                            pass
                return out

            try:
                with httpx.Client(timeout=timeout_long) as client:
                    data = _consume_sse_stream(client)
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.StreamClosed) as stream_err:
                # Common when proxies idle-timeout SSE during long LLM report (no bytes for minutes).
                logger.warning(
                    "Reconciliation SSE failed (%s); falling back to non-streaming from-bq",
                    stream_err,
                )
                if emitter:
                    emitter(
                        "Live progress stream dropped (network idle timeout). "
                        "Fetching the full report in one request — this may take several minutes with no interim updates…"
                    )
                with httpx.Client(timeout=timeout_long) as client:
                    r2 = client.post(json_url, json=payload)
                    r2.raise_for_status()
                    data = r2.json()
            except OSError as ose:
                if "incomplete" in str(ose).lower() or "chunked" in str(ose).lower():
                    logger.warning("Reconciliation stream OSError (%s); fallback to from-bq", ose)
                    if emitter:
                        emitter("Retrying reconciliation without the live progress stream…")
                    with httpx.Client(timeout=timeout_long) as client:
                        r2 = client.post(json_url, json=payload)
                        r2.raise_for_status()
                        data = r2.json()
                else:
                    raise

            if isinstance(data, dict) and data.get("__stream_error__"):
                return (str(data["__stream_error__"]), [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
            if not data:
                data = {}
            result_text = (data.get("final_md") or data.get("draft_md") or "").strip()
            summary = data.get("summary") or {}
            if summary:
                ib = summary.get("in_both_count", 0)
                ext = summary.get("external_only_count", 0)
                internal = summary.get("internal_only_count", 0)
                u_issues = int(summary.get("roster_upload_validation_issue_rows") or 0)
                u_fix = int(summary.get("roster_upload_issues_must_fix") or 0)
                u_ver = int(summary.get("roster_upload_issues_verify") or 0)
                sum_line = (
                    f"**Summary:** in_both={ib}, external_only={ext}, internal_only={internal}"
                )
                if u_issues:
                    sum_line += (
                        f"; **upload rows needing attention:** {u_issues} "
                        f"(must_fix={u_fix}, verify={u_ver})"
                    )
                header = f"Roster alignment with NPPES (Phase 1) for {org_name}\n\n{sum_line}\n\n---\n\n"
                if u_fix:
                    header = (
                        f"**Roster file:** {u_fix} line(s) need fixes (missing/invalid NPI or failed resolution). "
                        "See **Roster file — rows to fix** in the report and step CSV `roster_upload_validation_issues`.\n\n"
                    ) + header
                result_text = header + result_text
            if extra_out is not None and data:
                extra_out["report_run_id"] = (data.get("report_run_id") or "").strip()
                extra_out["last_report_org"] = org_name
                extra_out["roster_report_pdf_base64"] = (data.get("pdf_base64") or data.get("roster_report_pdf_base64") or "").strip()
                extra_out["roster_report_final_md"] = (data.get("final_md") or "").strip()
                step_outs = data.get("roster_step_outputs") or data.get("step_outputs") or []
                extra_out["roster_step_outputs"] = [
                    {
                        "step_id": s.get("step_id"),
                        "step_num": _ROSTER_STEP_OUTPUT_NUM.get((s.get("step_id") or "").strip(), 0),
                        "label": s.get("label"),
                        "csv_content": s.get("csv_content") or "",
                        "row_count": s.get("row_count", 0),
                        "markdown_content": (s.get("markdown_content") or "").strip(),
                        "json_content": (s.get("json_content") or "").strip(),
                    }
                    for s in step_outs
                ]
                extra_out["roster_report_attachments_kind"] = "reconciliation"
            if result_text:
                sources = [
                    {
                        "index": 1,
                        "document_name": "Roster alignment with NPPES (Phase 1)",
                        "text": result_text[:300],
                        "source_type": "external",
                    }
                ]
                return (result_text, sources, None, RETRIEVAL_SIGNAL_ROSTER_COMPLETE)
            return ("Reconciliation report returned no content.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
        except httpx.HTTPStatusError as e:
            try:
                body = (e.response.text or "")[:500]
            except httpx.ResponseNotRead:
                try:
                    e.response.read()
                    body = (e.response.text or "")[:500]
                except Exception:
                    body = ""
            return (f"Reconciliation API error: {e.response.status_code}. {body}", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
        except Exception as e:
            logger.warning("Reconciliation report failed: %s", e, exc_info=True)
            return (f"Reconciliation failed: {e}. Ensure roster is uploaded, processed, and loaded to BigQuery.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)

    # ── Credentialing: persisted report vs new report ──
    # We use a stored report (POST /report-runs/{id}/ask) when: credentialing intent AND NOT wants_new_report.
    # wants_new_report = phrases like "create a credentialing report for X", "roster report for X".
    # If credentialing_intent and not wants_new_report: resolve report_run_id (from state or GET latest by org),
    # then return _ask_credentialing_report(...). Otherwise we may run the full 11-step orchestrator (new report).
    # Blueprint also forces agent=reasoning when active_skill is roster_report and message refers to same org (no re-run).
    msg = (user_message or question or "").strip()
    msg_lower = msg.lower()
    roster_triggers_new = (
        "run roster reconciliation report for",
        "roster reconciliation report for",
        "reconciliation report for",
        "run reconciliation report for",
        "provider roster for",
        "credentialing report for",
        "roster report for",
        "medicaid roster for",
        "roster for",
        "create a medicaid npi report for",
        "create medicaid npi report for",
        "create a credentialing report for",
        "create credentialing report for",
        "medicaid npi report for",
    )
    wants_new_report = any(t in msg_lower for t in roster_triggers_new)
    # credentialing_qa = answer from report or generic only; never run the 11-step orchestrator
    if tool_hint_override and (tool_hint_override or "").strip().lower() == "credentialing_qa":
        wants_new_report = False
    report_run_id = (active_context or {}).get("report_run_id") if isinstance(active_context, dict) else None
    report_run_id = (report_run_id or "").strip() if isinstance(report_run_id, str) else None

    # Credentialing intent: must mention credentialing / NPI / roster report (not just "report").
    # Generic "the report" / "section c" only counts when we already have a run (same-thread follow-up).
    explicit_credentialing = (
        "latest report" in msg_lower or "report for " in msg_lower
        or "npi ready" in msg_lower or "ready for pml" in msg_lower or "why is this npi" in msg_lower
        or "npi valid" in msg_lower or "valid for florida" in msg_lower or "florida billing" in msg_lower
        or "is this npi" in msg_lower or "npi in the report" in msg_lower
        or "credentialing" in msg_lower or "nppes" in msg_lower
        or any(
            t in msg_lower
            for t in (
                "roster report",
                "roster reconciliation",
                "reconciliation report",
                "medicaid roster",
                "medicaid npi report",
            )
        )
    )
    report_followup_phrases = (
        "the report say", "the report says", "what does the report", "summarize section",
        "section c", "section b", "section d", "section a", "at-risk", "executive summary",
        "pml", "how many npi", "npis have", "issues with pml",
    )
    report_followup = any(t in msg_lower for t in report_followup_phrases)
    credentialing_intent = explicit_credentialing or (report_run_id and report_followup) or (
        bool((active_context or {}).get("last_report_org")) and report_followup
    )
    if tool_hint_override and (tool_hint_override or "").strip().lower() == "credentialing_qa":
        credentialing_intent = True

    if credentialing_intent and not wants_new_report:
        # Resolve run: from state, or pull up latest for org (from message or last_report_org).
        # Reports may not be persisted in thread; we "pull up" latest unless user asked for reload/new report.
        run_id_to_use = report_run_id
        org_name = None
        for prefix in (
            "latest report for ", "what is the latest report for ", "what's the latest report for ",
            "latest for ", "report for ", "credentialing report for ", "medicaid npi report for ",
            "tell me more about the credentialing report for ", "tell me about the credentialing report for ",
        ):
            if prefix in msg_lower:
                org_name = msg[msg_lower.find(prefix) + len(prefix):].strip().rstrip("?.,;!")
                break
        if not org_name and " for " in msg_lower and "report" in msg_lower:
            idx = msg_lower.rfind(" for ")
            if idx >= 0:
                org_name = msg[idx + 5:].strip().rstrip("?.,;!")
        if not org_name and isinstance(active_context, dict) and (active_context.get("last_report_org") or "").strip():
            org_name = (active_context.get("last_report_org") or "").strip()
        if org_name:
            org_name = _clean_org_name_for_credentialing(org_name)
        if not run_id_to_use and org_name and len(org_name) >= 2:
            _emit(emitter, f"Looking up latest report for {org_name}…")
            run = _get_latest_run_for_org(org_name)
            if run and run.get("report_run_id"):
                run_id_to_use = run["report_run_id"]
                if extra_out is not None:
                    extra_out["report_run_id"] = run_id_to_use
                    extra_out["last_report_org"] = org_name
            else:
                # Plan B: try org-name search candidates (e.g. "David Lawrence" → "David Lawrence Center")
                candidates = _get_org_name_candidates(org_name, limit=8, skill_search_mode=_org_skill_mode)
                for candidate in candidates:
                    if candidate == org_name:
                        continue
                    run = _get_latest_run_for_org(candidate)
                    if run and run.get("report_run_id"):
                        run_id_to_use = run["report_run_id"]
                        if extra_out is not None:
                            extra_out["report_run_id"] = run_id_to_use
                            extra_out["last_report_org"] = candidate
                        _emit(emitter, f"Using report for {candidate}.")
                        break
                if not run_id_to_use and credentialing_intent:
                    if candidates:
                        names = ", ".join(repr(c) for c in candidates[:5])
                        return (
                            f"No stored report found for {org_name!r}. Did you mean one of these: {names}? "
                            f"Say 'Create a credentialing report for [exact name]' to generate one.",
                            [],
                            None,
                            RETRIEVAL_SIGNAL_NO_SOURCES,
                        )
                    return (
                        f"No stored report found for {org_name!r}. "
                        f"Say 'Create a credentialing report for {org_name}' to generate one. "
                        "Or I can look up basic NPI info from NPPES (no PML status).",
                        [],
                        None,
                        RETRIEVAL_SIGNAL_NO_SOURCES,
                    )
        if run_id_to_use:
            if extra_out is not None and org_name and len(org_name) >= 2:
                extra_out["last_report_org"] = org_name
            elif extra_out is not None and isinstance(active_context, dict) and (active_context.get("last_report_org") or "").strip():
                extra_out["last_report_org"] = (active_context.get("last_report_org") or "").strip()
            _emit(emitter, "Your report is stored. You can ask any question — answering from it.")
            return _ask_credentialing_report(run_id_to_use, (question or user_message or "").strip(), emitter=emitter)
        if credentialing_intent:
            if tool_hint_override and (tool_hint_override or "").strip().lower() == "credentialing_qa":
                return (CREDENTIALING_QA_NO_REPORT, [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
            return (
                "I don't have a report in this thread. Say which organization, e.g. 'What is the latest report for David Lawrence Center?' or run a credentialing report first.",
                [],
                None,
                RETRIEVAL_SIGNAL_NO_SOURCES,
            )

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

        if hint in ("npi_lookup", "search_org_names"):
            # Entity from question ONLY — active payer is never the org being looked up
            org = entity.get('org_name') or entity.get('raw', '')[:80]
            if org and len(org.strip()) > 1:
                return _run_npi_lookup_by_name(
                    org.strip(),
                    emitter=emitter,
                    extract_candidate=(user_message or question or ""),
                    skill_search_mode=_org_skill_mode,
                    pipeline_ctx=pipeline_ctx,
                )
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

    # Provider Roster / Credentialing report: "provider roster for X", "credentialing report for X",
    # "create a Medicaid NPI report for X", "I want to create a credentialing report for X"
    roster_triggers = (
        "provider roster",
        "credentialing report",
        "roster report",
        "roster reconciliation",
        "reconciliation report",
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
            "run roster reconciliation report for",
            "roster reconciliation report for",
            "reconciliation report for",
            "run reconciliation report for",
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
            org_name = _clean_org_name_for_credentialing(org_name)
            if not org_name or len(org_name) < 2:
                return (
                    "I couldn't extract a clear organization name from your message. "
                    "Try: 'Create a credentialing report for Aspire Health' or 'Medicaid NPI report for David Lawrence Center'.",
                    [],
                    None,
                    RETRIEVAL_SIGNAL_NO_SOURCES,
                )
            # Don't run the full report when "org" is clearly a follow-up (e.g. "i meant section E of the credentialing report")
            if not _is_plausible_org_name(org_name):
                if isinstance(active_context, dict) and ((active_context.get("report_run_id") or "").strip() or (active_context.get("last_report_org") or "").strip()):
                    run_id = (active_context.get("report_run_id") or "").strip()
                    if not run_id and (active_context.get("last_report_org") or "").strip():
                        run = _get_latest_run_for_org((active_context.get("last_report_org") or "").strip())
                        if run and run.get("report_run_id"):
                            run_id = run["report_run_id"]
                    if run_id:
                        _emit(emitter, "Your report is stored. You can ask any question — answering from it.")
                        return _ask_credentialing_report(run_id, (question or roster_check_text or "").strip(), emitter=emitter)
                return (
                    CREDENTIALING_QA_NO_REPORT,
                    [],
                    None,
                    RETRIEVAL_SIGNAL_NO_SOURCES,
                )
            # "Reload and create credentialing report for X" / "reload data and run ..." → force FL Medicaid daily load first
            # Also: first NPI report of the day → auto reload (so reports use fresh PML/dbt data)
            reload_triggers = (
                "reload and",
                "reload data",
                "reload then",
                "force reload",
                "refresh data",
                "reload and create",
                "reload, then",
                "reload; then",
                "reload then create",
            )
            force_reload = bool((credentialing_options or {}).get("force_refresh")) or any(
                r in roster_lower for r in reload_triggers
            )
            run_reload = force_reload or _should_run_first_of_day_reload()
            if run_reload:
                _run_fl_medicaid_daily_load(emitter)

            # Subsequent same-day reports for this org → serve from cache (no full chain)
            skip_same_day_cache = bool((credentialing_options or {}).get("prefer_fresh_report"))
            existing_run = _get_latest_run_for_org(org_name)
            if (
                not skip_same_day_cache
                and existing_run
                and (existing_run.get("status") or "").lower() == "completed"
            ):
                created_at = existing_run.get("created_at") or ""
                if created_at:
                    try:
                        # Parse ISO datetime; compare date to today (UTC)
                        if "T" in str(created_at):
                            run_date = datetime.fromisoformat(
                                str(created_at).replace("Z", "+00:00")
                            ).date()
                        else:
                            run_date = datetime.strptime(
                                str(created_at)[:10], "%Y-%m-%d"
                            ).date()
                        today_utc = datetime.now(timezone.utc).date()
                        if run_date == today_utc:
                            _emit(emitter, f"Serving cached report for {org_name} (from earlier today).")
                            return _serve_cached_credentialing_report(
                                existing_run, org_name, extra_out
                            )
                    except (ValueError, TypeError):
                        pass

            _emit(emitter, f"Running the Medicaid NPI report for {org_name}…")
            try:
                active_merge: dict[str, Any] = {}
                if (thread_id or "").strip():
                    try:
                        from app.storage.threads import get_state

                        st = get_state((thread_id or "").strip())
                        active_merge = (st or {}).get("active") or {}
                    except Exception:
                        active_merge = {}
                # 2026-04-18 disconnect: credentialing_envelope deleted.
                # This branch is also unreachable (same dead-code story
                # as the roster_reconciliation branch above) — fallback
                # to empty merge context so dispatch won't crash if it
                # somehow gets here.
                uid3, ext3, incl3 = None, None, None
                result_text, ostate = run_orchestrator(
                    org_name,
                    emitter=emitter,
                    roster_upload_id=uid3,
                    external_only=ext3,
                    include_roster_members=incl3,
                )
            except Exception as e:
                logger.warning("run_orchestrator failed: %s", e, exc_info=True)
                return (f"I ran into an issue running the plan. {e}. Please try again.", [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
            failed_early = ostate.first_failed_step() if ostate else None
            if failed_early is not None:
                msg = (
                    f"Credentialing stopped at step **{failed_early.id}**: {failed_early.result_summary or 'failed'}. "
                    "Fix the provider-roster skill / org resolution and try again."
                )
                return (msg, [], None, RETRIEVAL_SIGNAL_NO_SOURCES)
            result_text = result_text or ""
            if extra_out is not None:
                step_order = dict(_ROSTER_STEP_OUTPUT_NUM)
                extra_out["roster_step_outputs"] = [
                    {
                        "step_id": s.step_id,
                        "step_num": step_order.get(s.step_id, 0),
                        "label": s.label,
                        "csv_content": s.csv_content,
                        "row_count": s.row_count,
                        "markdown_content": getattr(s, "markdown_content", "") or "",
                        "json_content": getattr(s, "json_content", "") or "",
                    }
                    for s in (ostate.step_outputs or [])
                ]
                if getattr(ostate, "report_run_id", None):
                    extra_out["report_run_id"] = ostate.report_run_id
                extra_out["last_report_org"] = org_name
                if getattr(ostate, "report_pdf_base64", None):
                    extra_out["roster_report_pdf_base64"] = ostate.report_pdf_base64
                if getattr(ostate, "report_final_md", None):
                    extra_out["roster_report_final_md"] = ostate.report_final_md
                extra_out["roster_report_attachments_kind"] = "credentialing"
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
        return _run_npi_lookup_by_name(
            org_name,
            emitter=emitter,
            extract_candidate=extract_candidate,
            skill_search_mode=_org_skill_mode,
            pipeline_ctx=pipeline_ctx,
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
