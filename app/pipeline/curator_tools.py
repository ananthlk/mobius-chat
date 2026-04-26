"""ReAct tool handlers for the curator (Phase 13.5).

Two tools live here, called from ``react_loop._execute_tool``:

* ``lookup_authoritative_sources`` — query rag's /sources/search to
  enumerate URLs Mobius has seen for a payer/state/topic. Returns
  both ingested and not-yet-ingested URLs.

* ``ingest_url`` — POST to rag's /documents/import-from-html (or
  /import-from-gcs for already-uploaded PDFs) to pull a single URL
  through the chunking + embedding + lexicon + publish pipeline.

Both are HTTP, not direct DB. Reasons:
* /sources/search runs on rag; the same shape works whether chat reads
  from the same Postgres or talks to a future curator service split out.
* ingest_url has side effects (chunking, embedding, publishing) that
  belong in rag's process; chat shouldn't reach across the wire to
  trigger them via SQL.

Configuration:
* ``RAG_API_URL`` env (already set in deploy/dev.env) is the base for
  rag's HTTP API. We reuse it; no new env vars.
* ``MOBIUS_RAG_ADMIN_KEY`` env (optional) — admin auth for write ops
  like ingest_url. If unset we still try the call; rag may 401 and the
  tool reports the failure cleanly.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Constants matching react_loop's expected return shape ────────────

# Borrowed from react_loop; keeping a local string to avoid an import
# cycle (react_loop imports this module).
_NO_SOURCES = "no_sources"
_CORPUS_HIT = "corpus_hit"


def _rag_base() -> str:
    base = (os.environ.get("RAG_API_URL") or "").strip().rstrip("/")
    if not base:
        # Fallback: many chat deploys also have RAG_API_BASE
        base = (os.environ.get("RAG_API_BASE") or "").strip().rstrip("/")
    return base


def _admin_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    key = (os.environ.get("MOBIUS_RAG_ADMIN_KEY") or os.environ.get("ADMIN_API_KEY") or "").strip()
    if key:
        headers["X-Admin-Api-Key"] = key
    return headers


# USPS state names → 2-letter code. The registry's `state` column
# stores the 2-letter form ("FL"), but the planner often passes the
# full name from ``active.jurisdiction`` ("Florida"). Without this
# normalization the registry returned 0 rows on every Sunshine/Florida
# curator call, surfacing as "no curated sources" — the RAG agent's
# /sources/search?state=FL returns the URL fine, but state=Florida
# doesn't match. Keep the table small and explicit; we can extend
# when other states show up in production traces.
_STATE_NAME_TO_CODE = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "puerto rico": "PR",
}


def _normalize_state(v: Any) -> str:
    """Return USPS 2-letter code; pass through if already 2 chars or unknown."""
    s = str(v or "").strip()
    if not s:
        return s
    if len(s) == 2:
        return s.upper()
    return _STATE_NAME_TO_CODE.get(s.lower(), s)


def _no_rag_url_result(tool: str) -> dict:
    return {
        "tool": tool,
        "success": False,
        "result": (
            "Curator tool unavailable: RAG_API_URL not configured. "
            "This deployment can't reach the rag service."
        ),
        "signal": _NO_SOURCES,
        "sources": [],
    }


# ── lookup_authoritative_sources ─────────────────────────────────────


def call_lookup_authoritative_sources(inputs: dict) -> dict:
    """GET /sources/search with the planner's filter inputs.

    Pass-through: payer / state / topic / authority_level. We always
    request only_reachable=True so the planner doesn't propose URLs
    we already know are 404 / 403. Limit caps at 20 because the
    planner's prompt window can't usefully digest more.
    """
    base = _rag_base()
    if not base:
        return _no_rag_url_result("lookup_authoritative_sources")

    params: dict[str, Any] = {"only_reachable": "true", "limit": 20}
    for key in ("payer", "state", "program", "topic", "authority_level"):
        v = inputs.get(key)
        if v:
            # 2026-04-26: the registry stores ``state`` as the 2-letter
            # USPS code ("FL"), but the planner often emits the full
            # name from ``active.jurisdiction`` ("Florida"). Without
            # normalization the SQL state= filter returns zero rows
            # even when the URL is in the registry — this was the
            # "no curated sources" dead-end on the dental-plan-
            # transition retest. Normalize on the chat side so the
            # RAG endpoint stays simple.
            if key == "state":
                v = _normalize_state(v)
            params[key] = v
    # Phase 13.5d — pass the topic ALSO as q= for BM25-style relevance
    # ranking on the registry's search_vector. topic= requires exact
    # tag match (often empty since topic_tags isn't pre-populated);
    # q= ranks by ts_rank over payer/path/host/authority text. With
    # both set, planner gets exact-tag hits when they exist AND
    # relevance-ranked fuzzy hits when they don't — best of both.
    topic_val = inputs.get("topic")
    if topic_val:
        params["q"] = topic_val
    # Caller can also pass an explicit free-text query distinct from
    # the topic tag (rare but useful — e.g., when the planner's
    # natural-language phrasing diverges from a one-word topic tag).
    explicit_q = inputs.get("q") or inputs.get("query")
    if explicit_q:
        params["q"] = explicit_q
    # Optional: caller can ask for only-non-ingested rows to surface
    # gaps the corpus doesn't cover yet.
    if inputs.get("non_ingested_only") is True:
        params["ingested"] = "false"

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{base}/sources/search", params=params)
            resp.raise_for_status()
            rows = resp.json() or []
    except httpx.HTTPStatusError as e:
        logger.warning("lookup_authoritative_sources HTTP %d: %s", e.response.status_code, e.response.text[:200])
        return {
            "tool": "lookup_authoritative_sources",
            "success": False,
            "result": f"Curator returned HTTP {e.response.status_code}",
            "signal": _NO_SOURCES,
            "sources": [],
        }
    except Exception as e:
        logger.exception("lookup_authoritative_sources failed")
        return {
            "tool": "lookup_authoritative_sources",
            "success": False,
            "result": f"Curator unreachable: {type(e).__name__}",
            "signal": _NO_SOURCES,
            "sources": [],
        }

    if not rows:
        return {
            "tool": "lookup_authoritative_sources",
            "success": True,
            "result": "Curator registry has no matching sources for this query.",
            "signal": _NO_SOURCES,
            "sources": [],
            "rows": [],
        }

    # Render a compact prose summary the planner can act on.
    # Format: one bullet per source with the action signal upfront.
    lines = [f"Curator registry returned {len(rows)} URL(s):"]
    ingested_count = sum(1 for r in rows if r.get("ingested"))
    not_ingested_count = len(rows) - ingested_count
    for r in rows[:15]:
        flag = "✓ indexed" if r.get("ingested") else "○ NOT indexed"
        auth = r.get("effective_authority_level") or "—"
        lines.append(
            f"- [{flag}] {r.get('url')}  "
            f"(payer={r.get('payer') or '—'}, state={r.get('state') or '—'}, "
            f"authority={auth}, last_seen={(r.get('last_seen_at') or '')[:10]})"
        )
    if len(rows) > 15:
        lines.append(f"  …and {len(rows)-15} more.")
    if not_ingested_count > 0:
        lines.append(
            f"\n{not_ingested_count} of these are NOT yet in the corpus. "
            "If the user wants one of them, call ingest_url(url) for that "
            "specific URL after confirming with the user."
        )
    summary = "\n".join(lines)

    return {
        "tool": "lookup_authoritative_sources",
        "success": True,
        "result": summary,
        "signal": _NO_SOURCES,  # this isn't a corpus retrieval; planner uses result text
        "sources": [],
        # Structured payload the planner can also reference programmatically.
        "rows": rows,
    }


# ── ingest_url ───────────────────────────────────────────────────────


def call_ingest_url(inputs: dict) -> dict:
    """POST /documents/import-from-html (or /import-from-gcs).

    For now we always route through import-from-html, which auto-fetches
    HTML and runs through the chunking pipeline. PDFs at that endpoint
    will return a clean error; future enhancement is to detect content-
    type and dispatch to /import-from-gcs (or a similar URL-based
    endpoint) when the URL is a PDF.
    """
    # Validate the planner-controlled input first — "you forgot the
    # url" is more actionable than "deploy isn't configured" when
    # the planner literally didn't pass a URL.
    url = (inputs.get("url") or "").strip()
    if not url:
        return {
            "tool": "ingest_url",
            "success": False,
            "result": "ingest_url requires a 'url' input.",
            "signal": _NO_SOURCES,
            "sources": [],
        }

    base = _rag_base()
    if not base:
        return _no_rag_url_result("ingest_url")

    # Caller can pass extra metadata if known; otherwise rag's classifier
    # infers from the URL host.
    body = {"url": url}
    for opt_key in ("title", "payer", "state", "program", "authority_level"):
        v = inputs.get(opt_key)
        if v:
            body[opt_key] = v

    try:
        with httpx.Client(timeout=120) as client:
            resp = client.post(
                f"{base}/documents/import-from-html",
                json=body,
                headers=_admin_headers(),
            )
            if resp.status_code == 409:
                # Already in the corpus — treat as success, the planner
                # should immediately call search_corpus.
                detail = resp.json().get("detail", {})
                doc_id = detail.get("document_id") if isinstance(detail, dict) else None
                return {
                    "tool": "ingest_url",
                    "success": True,
                    "result": (
                        f"URL was already in the corpus (document_id={doc_id}). "
                        "Call search_corpus with the original question to retrieve it."
                    ),
                    "signal": _CORPUS_HIT,
                    "sources": [],
                    "document_id": doc_id,
                }
            resp.raise_for_status()
            data = resp.json() or {}
    except httpx.HTTPStatusError as e:
        body_text = e.response.text[:300]
        logger.warning("ingest_url HTTP %d: %s", e.response.status_code, body_text)
        return {
            "tool": "ingest_url",
            "success": False,
            "result": f"Ingest failed: HTTP {e.response.status_code}. {body_text}",
            "signal": _NO_SOURCES,
            "sources": [],
        }
    except Exception as e:
        logger.exception("ingest_url failed")
        return {
            "tool": "ingest_url",
            "success": False,
            "result": f"Ingest failed: {type(e).__name__}: {e}",
            "signal": _NO_SOURCES,
            "sources": [],
        }

    doc_id = data.get("document_id")
    sections = data.get("sections")
    status = data.get("status") or "unknown"
    title = data.get("title") or url
    return {
        "tool": "ingest_url",
        "success": True,
        "result": (
            f"Ingested ‘{title}’ from {url}. document_id={doc_id}, "
            f"status={status}, sections={sections}. "
            "It will be queryable in chat after chunking + embedding + publish "
            "completes (typically a few minutes). Call search_corpus next to "
            "retrieve from it."
        ),
        "signal": _NO_SOURCES,  # newly ingested; not yet a corpus hit this turn
        "sources": [],
        "document_id": doc_id,
    }
