"""Download endpoints for the fetch_document skill.

Two file-streaming routes that complete the download-card story:

  GET /chat/uploads/{document_id}/download
      Stream a user's instant-rag upload back to them. Ownership-checked
      against the upload catalog, then proxied from mobius-rag's
      ``/documents/{id}/file`` (upload bytes live in rag's GCS bucket;
      chat never stores file bytes).

  GET /chat/download-proxy?url=...
      Server-side fetch of a web document so the browser gets a clean
      same-origin download (real filename, no CORS lottery). Guarded:
      http(s) only, host must exist in the curator's discovered_sources
      registry (fail-closed), resolved IPs must be public (SSRF), size
      capped, redirects re-validated hop by hop. Every request is
      audit-logged with the caller identity.

Cards emitted by ``fetch_document`` use RELATIVE URLs for both routes so
they resolve against whatever origin serves the chat frontend — no
public-base-URL env needed.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
import urllib.parse
from typing import Any, Iterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.front_door import auth_mode, require_user
from app.storage.instant_rag_catalog import get_by_document_id

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB
_MAX_REDIRECTS = 3
_FETCH_TIMEOUT_S = 60
_HOSTS_CACHE_TTL_S = 600

# Per-host registry-membership cache: host → (checked_at, known).
_host_cache: dict[str, tuple[float, bool]] = {}


def _rag_base() -> str:
    return (
        os.environ.get("RAG_API_URL") or os.environ.get("RAG_API_BASE") or ""
    ).strip().rstrip("/")


def _safe_filename(name: str, fallback: str = "document") -> str:
    name = (name or "").strip().replace('"', "").replace("\r", "").replace("\n", "")
    return name or fallback


# ── Upload download ─────────────────────────────────────────────────


@router.get("/chat/uploads/{document_id}/download")
def download_upload(
    document_id: str,
    user_id: str | None = Depends(require_user),
) -> StreamingResponse:
    """Stream an instant-rag upload's original bytes back to its owner."""
    row = get_by_document_id(document_id)
    if not row:
        raise HTTPException(status_code=404, detail="Upload not found in catalog.")

    # Ownership: same policy as link_upload_to_thread — enforced when
    # auth is required, skipped in dev auth-off/optional modes.
    if auth_mode() == "required":
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required.")
        if row.get("user_id") and row.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="This upload belongs to another user.")

    base = _rag_base()
    if not base:
        raise HTTPException(status_code=503, detail="RAG service not configured.")

    upstream_url = f"{base}/documents/{urllib.parse.quote(document_id)}/file"
    filename = _safe_filename(row.get("filename") or "", fallback="upload")

    logger.info(
        "download: upload document_id=%s user=%s filename=%s",
        document_id, user_id or "-", filename,
    )
    return _stream_upstream(upstream_url, filename=filename)


# ── Web download proxy ──────────────────────────────────────────────


def _host_in_registry(host: str) -> bool:
    """True when the curator registry has at least one source on this
    host. Checked via ``/sources/search?host=`` (NOT ``/sources/stats``
    — its by_host is a top-20 dashboard aggregate, not an index).
    Fail-closed: registry unreachable → not allowed."""
    host = (host or "").strip().lower()
    if not host:
        return False
    now = time.time()
    cached = _host_cache.get(host)
    if cached and now - cached[0] < _HOSTS_CACHE_TTL_S:
        return cached[1]
    base = _rag_base()
    if not base:
        return False
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{base}/sources/search",
                params={"host": host, "only_reachable": "false", "limit": 1},
            )
            resp.raise_for_status()
            known = bool(resp.json())
    except Exception as e:
        logger.warning("download-proxy: registry host check failed for %s: %s", host, e)
        return cached[1] if cached else False
    _host_cache[host] = (now, known)
    return known


def _host_resolves_public(host: str) -> bool:
    """SSRF guard: every resolved address must be public unicast."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return False
    return True


def _validate_proxy_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Only http(s) URLs can be proxied.")
    host = (parsed.hostname or "").lower()
    if not _host_in_registry(host):
        raise HTTPException(
            status_code=403,
            detail="URL host is not in Mobius's source registry; open the link directly instead.",
        )
    if not _host_resolves_public(host):
        raise HTTPException(status_code=403, detail="URL host does not resolve to a public address.")
    return parsed


@router.get("/chat/download-proxy")
def download_proxy(
    url: str = Query(..., description="Registry-known http(s) document URL"),
    user_id: str | None = Depends(require_user),
) -> StreamingResponse:
    """Fetch a registry-known web document server-side and stream it back."""
    current = url.strip()
    parsed = _validate_proxy_url(current)

    # Follow redirects manually so every hop is re-validated against the
    # registry allowlist + SSRF checks (follow_redirects=True would let a
    # known host bounce us to an internal address).
    for _ in range(_MAX_REDIRECTS):
        head_client = httpx.Client(timeout=_FETCH_TIMEOUT_S, follow_redirects=False)
        try:
            probe = head_client.send(
                head_client.build_request("GET", current), stream=True
            )
            if probe.status_code in (301, 302, 303, 307, 308):
                location = probe.headers.get("location") or ""
                probe.close()
                head_client.close()
                current = urllib.parse.urljoin(current, location)
                parsed = _validate_proxy_url(current)
                continue
            if probe.status_code != 200:
                code = probe.status_code
                probe.close()
                head_client.close()
                raise HTTPException(
                    status_code=502,
                    detail=f"Source site returned HTTP {code}.",
                )
            break
        except httpx.HTTPError as e:
            head_client.close()
            raise HTTPException(status_code=502, detail=f"Source fetch failed: {type(e).__name__}") from e
    else:
        raise HTTPException(status_code=502, detail="Too many redirects.")

    content_length = probe.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_DOWNLOAD_BYTES:
        probe.close()
        head_client.close()
        raise HTTPException(status_code=413, detail="File exceeds the 100 MB proxy limit.")

    content_type = probe.headers.get("content-type") or "application/octet-stream"
    basename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]).strip()
    filename = _safe_filename(basename, fallback="download")

    logger.info(
        "download: proxy user=%s url=%s content_type=%s length=%s",
        user_id or "-", current, content_type, content_length or "?",
    )

    def _stream() -> Iterator[bytes]:
        sent = 0
        try:
            for chunk in probe.iter_bytes(chunk_size=256 * 1024):
                sent += len(chunk)
                if sent > _MAX_DOWNLOAD_BYTES:
                    logger.warning("download-proxy: aborting %s at %d bytes (cap)", current, sent)
                    break
                yield chunk
        finally:
            probe.close()
            head_client.close()

    return StreamingResponse(
        _stream(),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Shared upstream streamer ────────────────────────────────────────


def _stream_upstream(upstream_url: str, *, filename: str) -> StreamingResponse:
    """Open a streaming GET to a trusted internal upstream and relay it."""
    client = httpx.Client(timeout=_FETCH_TIMEOUT_S, follow_redirects=True)
    try:
        resp = client.send(client.build_request("GET", upstream_url), stream=True)
    except httpx.HTTPError as e:
        client.close()
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {type(e).__name__}") from e
    if resp.status_code == 404:
        resp.close()
        client.close()
        raise HTTPException(status_code=404, detail="No original file available for this document.")
    if resp.status_code != 200:
        code = resp.status_code
        resp.close()
        client.close()
        raise HTTPException(status_code=502, detail=f"Upstream returned HTTP {code}.")

    content_type = resp.headers.get("content-type") or "application/octet-stream"

    def _stream() -> Iterator[bytes]:
        try:
            for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                yield chunk
        finally:
            resp.close()
            client.close()

    return StreamingResponse(
        _stream(),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
