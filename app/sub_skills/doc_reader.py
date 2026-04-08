"""Thin HTTP client for the doc-reader skill (port 8018).

Same pattern as task_management.py — stdlib-only, non-fatal.
Chat pipeline calls these to enrich RAG responses with structured
ReadEnvelope citations and sections.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_DOC_READER_DEFAULT = "http://localhost:8018"


def _doc_reader_base() -> str:
    return (
        os.environ.get("CHAT_SKILLS_DOC_READER_URL") or _DOC_READER_DEFAULT
    ).rstrip("/")


def _http_post(url: str, body: dict) -> dict[str, Any]:
    data = json.dumps(body, default=str).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return _http_post(f"{_doc_reader_base()}{path}", payload)
    except Exception as exc:
        logger.debug("doc_reader._post %s failed (non-fatal): %s", path, exc)
        return None


def extract(
    document_id: str,
    query: str,
    max_sections: int = 5,
    caller_id: str = "chat",
    run_id: str | None = None,
    org: str | None = None,
) -> dict[str, Any] | None:
    """Call doc-reader /extract. Non-fatal — returns None on failure."""
    return _post("/extract", {
        "document_id": document_id,
        "query": query,
        "max_sections": max_sections,
        "caller_id": caller_id,
        "run_id": run_id,
        "org": org,
    })


def read(
    document_id: str,
    view: str = "full",
    section_filter: str | None = None,
    tag_filter: dict | None = None,
    caller_id: str = "chat",
) -> dict[str, Any] | None:
    """Call doc-reader /read. Non-fatal."""
    payload: dict[str, Any] = {
        "document_id": document_id,
        "view": view,
        "caller_id": caller_id,
    }
    if section_filter:
        payload["section_filter"] = section_filter
    if tag_filter:
        payload["tag_filter"] = tag_filter
    return _post("/read", payload)


def summarize(
    document_id: str,
    caller_id: str = "chat",
) -> dict[str, Any] | None:
    """Call doc-reader /summarize. Non-fatal."""
    return _post("/summarize", {
        "document_id": document_id,
        "caller_id": caller_id,
    })


def read_upload(
    file_bytes: bytes,
    filename: str,
    content_type: str = "application/octet-stream",
    query: str | None = None,
) -> dict[str, Any] | None:
    """Upload a file to doc-reader for transient parsing. Non-fatal."""
    import urllib.request
    import io
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex[:16]}"
    body_parts = []
    body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode())
    body_parts.append(file_bytes)
    body_parts.append(b"\r\n")
    if query:
        body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"query\"\r\n\r\n{query}\r\n".encode())
    body_parts.append(f"--{boundary}--\r\n".encode())
    data = b"".join(body_parts)
    try:
        req = urllib.request.Request(
            f"{_doc_reader_base()}/read-upload",
            data=data,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.debug("doc_reader.read_upload failed (non-fatal): %s", exc)
        return None


def read_envelope_to_blocks(read_envelope: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Convert a doc-reader ReadEnvelope dict into assistant_envelope blocks + source refs.

    Returns (detail_blocks, source_refs) ready to merge into an assistant envelope.
    """
    blocks: list[dict[str, Any]] = []
    sections = read_envelope.get("sections") or []

    for sec in sections:
        md = sec.get("body_markdown", "")
        title = sec.get("heading", "")
        if md:
            blocks.append({
                "type": "detail",
                "markdown": f"**{title}**\n\n{md}" if title else md,
                "collapsed_default": True,
            })

    # Build source refs from citations across all sections
    refs: list[dict[str, Any]] = []
    seen_chunks: set[str] = set()
    for sec in sections:
        for cite in (sec.get("citations") or []):
            cid = cite.get("chunk_id", "")
            if cid in seen_chunks:
                continue
            seen_chunks.add(cid)
            ref: dict[str, Any] = {
                "index": len(refs),
                "title": cite.get("display") or read_envelope.get("display_name", "Source"),
                "page": cite.get("page"),
                "snippet": (cite.get("snippet") or "")[:400],
            }
            if cite.get("document_id"):
                ref["document_id"] = cite["document_id"]
            refs.append(ref)

    return blocks, refs
