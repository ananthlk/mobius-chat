"""Builtin skill: ``fetch_document`` — resolve a corpus document by
name / filename / policy ID and return a download link.

Distinct from ``search_corpus`` (which answers a question using
chunks from many docs) and ``search_uploaded_document`` (which scopes
to a specific user upload). This skill is for the planner intent
"the user wants the FILE itself, not the answer in it."

Use cases (planner-driven):
- "Send me the Sunshine Provider Manual"
- "I need FL.UM.87 PDF"
- "Download the prior-auth form"
- "Give me a copy of CC.PP.501"

Resolution order (against ``published_rag_metadata``):
  1. Substring + word-overlap match on ``document_display_name``
  2. Match on ``document_filename`` (handles "FL.UM.87" / ".pdf" / etc.)
  3. Tie-break: prefer most-recent ``updated_at`` on equal-rank matches
  4. Threshold: lowest-scoring tied result must have ≥ 2 query-token
     overlaps to count as a match. Below the threshold → semantic
     fallback via mobius-rag ``corpus_search`` (covers "the policy
     about telehealth visits" where the title never says telehealth).

Returns a SkillEnvelope with:
- ``text``: short confirmation line for the integrator to render
- ``sources``: one SourceRef per matched doc (citation panel keeps
  working for MCP callers and older frontends)
- a structured payload attached to
  ``pipeline_ctx.react_document_download_data``; ``integrate.py``
  turns it into a ``document_download`` envelope block the frontend
  renders as download cards.

Download URLs point at the ORIGINAL file bytes
(``/documents/{id}/file``, streamed from GCS) with the
text-reconstructed ``/documents/{id}/download/pdf`` as
``fallback_download_url`` for scraped / text-only docs that have no
binary original. The frontend tries them in that order.

PHI / safety: this skill returns metadata + a link only. It does NOT
fetch or re-stream the file. Auth/audit live on the mobius-rag side.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Any

from app.skills.registry import (
    SkillCall,
    SkillEnvelope,
    SkillSpec,
    SourceRef,
    register,
)

logger = logging.getLogger(__name__)


# ── RAG API base for download URLs ───────────────────────────────────


_DEFAULT_RAG_API = "http://localhost:8030"


def _rag_api_base() -> str:
    """Where mobius-rag serves /documents/{id}/download/pdf.

    Falls back to localhost for in-process tests. Cloud Run sets the
    real URL via ``RAG_API_BASE``. (Kept distinct from
    ``RAG_APP_BASE`` — the app is the SPA shell, the api is the
    document-serving HTTP backend.)
    """
    return (os.environ.get("RAG_API_BASE") or _DEFAULT_RAG_API).rstrip("/")


def _download_url(document_id: str) -> str:
    """Original file bytes streamed from GCS (404s for text-only docs)."""
    return f"{_rag_api_base()}/documents/{document_id}/file"


def _fallback_download_url(document_id: str) -> str:
    """PDF reconstructed from extracted page text — always available."""
    return f"{_rag_api_base()}/documents/{document_id}/download/pdf"


# ── Query parsing + scoring ─────────────────────────────────────────


# Stop words / fetch-intent verbs we strip before fuzzy matching.
# Keeps "send me the Sunshine Provider Manual" from matching docs
# whose names share "send / me / the".
_STOPWORDS = frozenset({
    "give", "send", "fetch", "download", "share", "get", "grab",
    "me", "us", "my", "the", "a", "an", "of", "for", "to",
    "i", "need", "want", "please", "pls", "copy", "file", "pdf",
    "document", "doc", "version", "latest",
})


def _tokenize(s: str) -> list[str]:
    """Lowercase, alphanumeric tokens, drop stopwords."""
    out: list[str] = []
    for tok in re.findall(r"[A-Za-z0-9.]+", (s or "").lower()):
        # Keep policy-ID-style dotted tokens whole ("FL.UM.87"), but
        # also split bare alphanum tokens normally.
        if tok in _STOPWORDS:
            continue
        if len(tok) <= 1:
            continue
        out.append(tok)
    return out


def _score_doc(
    query_tokens: list[str], display_name: str, filename: str, payer: str = ""
) -> tuple[int, int]:
    """Return (rank_overlap, len_penalty) for sorting (desc, asc).

    rank_overlap   — count of query tokens found in display_name,
                     filename, OR payer. Payer matters: the corpus is
                     full of docs named just "Provider_Manual.pdf"
                     whose payer column carries the "Sunshine Health"
                     the user actually said.
    len_penalty    — len(display_name) when matched (shorter = better tie-break)
    """
    if not query_tokens:
        return (0, 0)
    name_tokens = set(_tokenize(display_name))
    file_tokens = set(_tokenize(filename))
    payer_tokens = set(_tokenize(payer))
    target = name_tokens | file_tokens | payer_tokens
    overlap = sum(1 for t in query_tokens if t in target)
    return (overlap, len(display_name or "") + len(filename or ""))


# ── Postgres lookup ─────────────────────────────────────────────────


def _fetch_candidates(query: str, *, limit: int = 30) -> list[dict[str, Any]]:
    """Pull document candidates from Postgres metadata.

    The coarse token filter MUST live in SQL: the table holds ~9k
    distinct docs and ``db_query`` caps at 1000 rows, so an unfiltered
    scan silently ranks an arbitrary UUID-ordered subset (this is how
    "Sunshine provider manual" missed Sunshine's Provider_Manual.pdf).
    Any-token ILIKE over name/filename/payer keeps recall high; the
    Python ranking above stays the precision layer.
    """
    from app.db_client import db_query

    # Tokens are alphanumeric+dots only (see _tokenize), so no LIKE
    # metacharacter escaping is needed.
    patterns = [f"%{t}%" for t in _tokenize(query)[:8]]
    where = "document_id IS NOT NULL"
    params: dict[str, Any] = {}
    if patterns:
        where += (
            " AND (document_display_name ILIKE ANY(%(patterns)s)"
            " OR document_filename ILIKE ANY(%(patterns)s)"
            " OR document_payer ILIKE ANY(%(patterns)s))"
        )
        params["patterns"] = patterns
    sql = f"""
        SELECT DISTINCT ON (document_id)
            document_id::text AS document_id,
            document_display_name,
            document_filename,
            document_payer,
            document_state,
            document_program,
            document_authority_level,
            updated_at
        FROM published_rag_metadata
        WHERE {where}
        ORDER BY document_id, updated_at DESC
    """
    result = db_query(sql, "chat", params=params)
    if isinstance(result, dict) and result.get("error"):
        logger.warning("fetch_document: db_query error %s", result.get("error"))
        return []
    # db_query returns one of two shapes:
    #   { "rows": [{...}, ...] }                  (db-agent / dict rows)
    #   { "columns": [...], "rows": [[...], ...] }  (direct psycopg2 fallback)
    # Normalize both to list[dict].
    if not isinstance(result, dict):
        return []
    raw_rows = result.get("rows") or []
    if not raw_rows:
        return []
    if isinstance(raw_rows[0], dict):
        return [r for r in raw_rows if isinstance(r, dict)]
    cols = result.get("columns") or []
    if not cols:
        return []
    return [dict(zip(cols, r)) for r in raw_rows if isinstance(r, (list, tuple)) and len(r) == len(cols)]


def _rank_matches(query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank candidates by token overlap; filter below floor."""
    qtokens = _tokenize(query)
    if not qtokens:
        return []
    scored: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for c in candidates:
        score = _score_doc(
            qtokens,
            c.get("document_display_name") or "",
            c.get("document_filename") or "",
            c.get("document_payer") or "",
        )
        if score[0] >= 2 or (score[0] >= 1 and len(qtokens) <= 2):
            # Single-token queries (e.g. "FL.UM.87") get a relaxed floor
            # so a single strong hit still resolves.
            scored.append((score, c))
    scored.sort(key=lambda x: (-x[0][0], x[0][1]))
    return [c for _, c in scored]


# ── Semantic fallback via corpus_search ─────────────────────────────


def _corpus_search_resolve(query: str, *, limit: int = 3) -> list[dict[str, Any]]:
    """Resolve doc candidates semantically when name matching fails.

    "The policy about telehealth visits" won't token-overlap a title
    like "FL.UM.87 Utilization Management"; corpus_search finds the
    chunks and we dedupe their document_ids. Same RAG_API_URL knob the
    search_corpus skill uses (RAG_API_BASE fallback keeps single-env
    dev setups working)."""
    base = (
        os.environ.get("RAG_API_URL") or os.environ.get("RAG_API_BASE") or ""
    ).strip().rstrip("/")
    if not base:
        return []
    req = urllib.request.Request(
        f"{base}/api/skills/v1/corpus_search",
        data=json.dumps({"query": query, "k": 10}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    seen: set[str] = set()
    docs: list[dict[str, Any]] = []
    for chunk in payload.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        doc_id = str(chunk.get("document_id") or "").strip()
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        docs.append({
            "document_id": doc_id,
            "document_display_name": chunk.get("document_name") or "",
            "document_filename": chunk.get("document_filename") or "",
        })
        if len(docs) >= limit:
            break
    return docs


def _merge_metadata(
    resolved: list[dict[str, Any]], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Fill payer/state/program/authority on corpus-resolved docs from
    the metadata rows we already pulled (chunks don't carry them)."""
    by_id = {c.get("document_id"): c for c in candidates if c.get("document_id")}
    out: list[dict[str, Any]] = []
    for doc in resolved:
        meta = by_id.get(doc.get("document_id")) or {}
        merged = {**meta, **{k: v for k, v in doc.items() if v}}
        out.append(merged)
    return out


# ── Handler ─────────────────────────────────────────────────────────


def _attach_download_payload(
    call: SkillCall, documents: list[dict[str, Any]], query: str
) -> None:
    """Write the structured payload to
    ``pipeline_ctx.react_document_download_data``; ``integrate.py``
    injects it as a ``document_download`` envelope block (same path
    task skills use for ``react_task_list_data``). No-op when the
    dispatcher didn't pass a pipeline context (MCP standalone call)."""
    ctx = call.pipeline_ctx
    if ctx is None:
        return
    try:
        ctx.react_document_download_data = {"documents": documents, "query": query}
    except Exception as e:  # pragma: no cover — context is loose-typed
        logger.debug("attach react_document_download_data failed (non-fatal): %s", e)


def _run_fetch_document(call: SkillCall) -> SkillEnvelope:
    inputs = call.inputs or {}
    query = (inputs.get("query") or call.question or "").strip()
    if not query:
        return SkillEnvelope(
            text="No document query provided.",
            signal="no_sources",
        )

    try:
        candidates = _fetch_candidates(query)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("fetch_document: candidate fetch failed: %s", exc)
        return SkillEnvelope(
            text=f"Couldn't query the document index ({exc}).",
            signal="tool_error",
        )

    matches = _rank_matches(query, candidates)
    resolved_via = "name_match"
    if not matches:
        try:
            matches = _merge_metadata(_corpus_search_resolve(query), candidates)
            resolved_via = "corpus_search"
        except Exception as exc:
            logger.warning("fetch_document: corpus_search fallback failed: %s", exc)
            matches = []
    if not matches:
        return SkillEnvelope(
            text=(
                "I don't see a document matching that in our materials. "
                "If you have a copy, you can attach it to this thread."
            ),
            signal="no_sources",
        )

    # Top 3 — usually 1, but if the user said "Sunshine" we may have
    # both Provider Manual and Member Handbook. Multi-match renders as
    # a pick-list of download cards.
    top = matches[:3]
    sources: list[SourceRef] = []
    download_docs: list[dict[str, Any]] = []
    for m in top:
        doc_id = m.get("document_id") or ""
        if not doc_id:
            continue
        display = m.get("document_display_name") or m.get("document_filename") or "document"
        common = {
            "download_url": _download_url(doc_id),
            "fallback_download_url": _fallback_download_url(doc_id),
            "filename": m.get("document_filename") or "",
            "payer": m.get("document_payer") or "",
            "state": m.get("document_state") or "",
            "program": m.get("document_program") or "",
            "authority_level": m.get("document_authority_level") or "",
        }
        sources.append(SourceRef(
            document_name=display,
            document_id=doc_id,
            source_type="document",
            page_number=None,
            index=len(sources) + 1,
            text=(m.get("document_filename") or "") or display,
            authority="corpus",
            extra={"fetch_intent": True, **common},
        ))
        download_docs.append({
            "document_id": doc_id,
            "title": display,
            "resolved_via": resolved_via,
            **common,
        })

    _attach_download_payload(call, download_docs, query)

    if len(sources) == 1:
        text = f"Found **{sources[0].document_name}**. Use the card below to download it."
    else:
        names = ", ".join(s.document_name for s in sources[:3])
        text = (
            f"Found {len(sources)} possible matches: {names}. "
            "Pick the one you want from the cards below."
        )

    return SkillEnvelope(
        text=text,
        signal="ok",
        sources=sources,
        extra={
            "fetch_intent": True,
            "match_count": len(sources),
            "resolved_via": resolved_via,
            "document_download_payload": {"documents": download_docs, "query": query},
        },
    )


# ── Registration ────────────────────────────────────────────────────


register(
    SkillSpec(
        name="fetch_document",
        description=(
            "Resolve a corpus document by name / filename / policy ID and "
            "return a download link. Use this when the user wants the FILE "
            "itself, not the answer in it.\n"
            "Use when: phrases like 'send me', 'give me', 'download', 'fetch', "
            "'I need the …' followed by a document reference (display name, "
            "filename, policy code).\n"
            "Do NOT use when: the user asks a question that needs an answer "
            "from the doc (use search_corpus). Do NOT use for user uploads "
            "(use search_uploaded_document or list_thread_document_uploads).\n"
            "Returns: matched document metadata + a download URL the frontend "
            "renders as a clickable Download button."
        ),
        inputs_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The user's document reference — name, filename, "
                        "or policy ID. Stopwords (the/a/give/send/etc.) "
                        "are stripped before matching."
                    ),
                },
            },
            "required": ["query"],
        },
        handler=_run_fetch_document,
        requires_jurisdiction=False,
        follow_up_capable=False,
        visible_to_planner=True,
        category="documents",
        display_name="Fetch Document",
    )
)


__all__ = ["_run_fetch_document"]
