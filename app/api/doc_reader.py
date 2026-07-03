"""Doc-reader skill proxy router — extracted from main.py (Phase 2b).

Mounts four routes that forward chat → mobius-skills/doc-reader:

    POST /chat/doc-reader/read       — read/reassemble a published doc
    POST /chat/doc-reader/extract    — query-targeted extraction
    POST /chat/doc-reader/summarize  — LLM summary
    GET  /chat/doc-reader/health     — upstream health check

All four POST endpoints require auth (Phase 2d via ``require_user``);
``/health`` does not so monitoring can probe the upstream without a
JWT.

Upstream URL comes from ``CHAT_SKILLS_DOC_READER_URL`` env
(fallback: ``http://localhost:8018``). The ``_doc_reader_proxy`` helper
maps upstream failures to ``HTTPException(502)`` so the client gets a
consistent error shape regardless of whether the skill is down, 5xx'ing,
or returning non-JSON.

History: these routes lived inline in app/main.py from inception; Phase
2b extracts them to shrink main.py and keep doc-reader concerns in one
file (proxy + routes together). External URLs unchanged because main.py
now does ``app.include_router(doc_reader.router)``.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.front_door import require_user

router = APIRouter(tags=["doc-reader"])

# Exposed so tests can patch the default without env contamination.
_DEFAULT_DOC_READER_URL = "http://localhost:8018"


def _doc_reader_base_url() -> str:
    """Resolve the upstream doc-reader skill URL.

    Moved into a helper (was inline in main.py) so tests can stub the
    base URL via monkeypatching this function rather than the env,
    which keeps test setup local to the unit under test."""
    return (os.environ.get("CHAT_SKILLS_DOC_READER_URL") or _DEFAULT_DOC_READER_URL).rstrip("/")


def _product_docs_base_url() -> str:
    """Base URL of the standalone product-awareness service, derived from the
    product_help_search URL (``…/search`` → ``…``). Product docs render from its
    ``/doc`` endpoint, NOT the RAG doc-reader, so they stay out of rag.documents."""
    url = (os.environ.get("CHAT_SKILLS_PRODUCT_HELP_SEARCH_URL")
           or "http://localhost:8070/search").rstrip("/")
    return url.rsplit("/search", 1)[0].rstrip("/") or url


def _doc_reader_proxy(
    method: str,
    path: str,
    *,
    json_body: Any | None = None,
    timeout: float = 30.0,
    base: str | None = None,
) -> dict[str, Any]:
    """Forward a request to the doc-reader skill and return its JSON.

    Any upstream failure (connection error, non-2xx status, non-JSON
    body) becomes ``HTTPException(502)`` so the browser / caller gets
    a clean "upstream error" without leaking stack traces or partial
    HTML from the upstream. ``HTTPException`` propagates untouched so
    this helper is safe to call from endpoints that raise 4xx for
    their own validation reasons.
    """
    import httpx

    base = (base or _doc_reader_base_url()).rstrip("/")
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method, f"{base}{path}", json=json_body)
            resp.raise_for_status()
            return resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Doc-reader skill error: {e}",
        ) from e


# ── Routes ────────────────────────────────────────────────────────────


@router.post("/chat/doc-reader/read")
def dr_read(
    body: dict = Body(...),
    _user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Proxy: read/reassemble a published document.

    Product-doc citations carry a ``product-docs:<module>`` document_id — those
    render from the standalone product-awareness service's ``/doc`` endpoint
    (same envelope shape), keeping product docs out of rag.documents. Everything
    else goes to the RAG doc-reader skill as before.
    """
    doc_id = (body.get("document_id") or "") if isinstance(body, dict) else ""
    if isinstance(doc_id, str) and doc_id.startswith("product-docs:"):
        return _doc_reader_proxy("POST", "/doc", json_body=body,
                                 base=_product_docs_base_url())
    return _doc_reader_proxy("POST", "/read", json_body=body)


@router.post("/chat/doc-reader/extract")
def dr_extract(
    body: dict = Body(...),
    _user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Proxy: query-targeted extraction from a document.

    Longer default timeout (60s vs. the default 30s) because extraction
    runs retrieval + re-ranking upstream and can take 20-40s on
    multi-hundred-page documents.
    """
    return _doc_reader_proxy("POST", "/extract", json_body=body, timeout=60.0)


@router.post("/chat/doc-reader/summarize")
def dr_summarize(
    body: dict = Body(...),
    _user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Proxy: generate LLM summary of a document.

    60s timeout mirrors the extraction path — summarization runs an
    LLM pass that matches extraction's worst-case latency.
    """
    return _doc_reader_proxy("POST", "/summarize", json_body=body, timeout=60.0)


@router.get("/chat/doc-reader/health")
def dr_health() -> dict[str, Any]:
    """Proxy: doc-reader health check.

    Intentionally not auth'd — monitoring + readiness probes need to
    reach this without carrying a user JWT.
    """
    return _doc_reader_proxy("GET", "/health")
