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
     overlaps to count as a match. Below the threshold → "no match"
     with optional suggestions.

Returns a SkillEnvelope with:
- ``text``: short confirmation line for the integrator to render
- ``sources``: one SourceRef per matched doc with:
    * document_id, document_name, source_type='document'
    * extra.download_url (mobius-rag /documents/{id}/download/pdf)
    * extra.fetch_intent=True so the frontend renders Download CTA
      instead of the usual citation snippet UI

The frontend reads ``download_url`` from each source's extras and
renders a 📥 Download button. ``RAG_API_BASE`` is needed in
``window`` for the URL to resolve at click time (set in index.html).

PHI / safety: this skill returns metadata + a link only. It does NOT
fetch or re-stream the file. Auth/audit live on the mobius-rag side.
"""
from __future__ import annotations

import logging
import os
import re
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


def _score_doc(query_tokens: list[str], display_name: str, filename: str) -> tuple[int, int]:
    """Return (rank_overlap, len_penalty) for sorting (desc, asc).

    rank_overlap   — count of query tokens found in display_name OR filename
    len_penalty    — len(display_name) when matched (shorter = better tie-break)
    """
    if not query_tokens:
        return (0, 0)
    name_tokens = set(_tokenize(display_name))
    file_tokens = set(_tokenize(filename))
    target = name_tokens | file_tokens
    overlap = sum(1 for t in query_tokens if t in target)
    return (overlap, len(display_name or "") + len(filename or ""))


# ── Postgres lookup ─────────────────────────────────────────────────


def _fetch_candidates(query: str, *, limit: int = 30) -> list[dict[str, Any]]:
    """Pull document candidates from Postgres metadata.

    We deliberately fetch a wider set than ``limit`` of distinct docs
    and rank in Python — Postgres trigram / full-text isn't worth the
    schema dependency for a small skill.
    """
    from app.db_client import db_query

    sql = """
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
        WHERE document_id IS NOT NULL
        ORDER BY document_id, updated_at DESC
    """
    result = db_query(sql, "chat", params={})
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
        )
        if score[0] >= 2 or (score[0] >= 1 and len(qtokens) <= 2):
            # Single-token queries (e.g. "FL.UM.87") get a relaxed floor
            # so a single strong hit still resolves.
            scored.append((score, c))
    scored.sort(key=lambda x: (-x[0][0], x[0][1]))
    return [c for _, c in scored]


# ── Handler ─────────────────────────────────────────────────────────


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
    if not matches:
        return SkillEnvelope(
            text=(
                "I don't see a document matching that in our materials. "
                "If you have a copy, you can attach it to this thread."
            ),
            signal="no_sources",
        )

    # Top 3 — usually 1, but if the user said "Sunshine" we may have
    # both Provider Manual and Member Handbook. Let the integrator
    # choose how to render multi-result.
    top = matches[:3]
    sources: list[SourceRef] = []
    for m in top:
        doc_id = m.get("document_id") or ""
        if not doc_id:
            continue
        display = m.get("document_display_name") or m.get("document_filename") or "document"
        sources.append(SourceRef(
            document_name=display,
            document_id=doc_id,
            source_type="document",
            page_number=None,
            index=len(sources) + 1,
            text=(m.get("document_filename") or "") or display,
            authority="corpus",
            extra={
                "download_url": _download_url(doc_id),
                "fetch_intent": True,
                "filename": m.get("document_filename") or "",
                "payer": m.get("document_payer") or "",
                "state": m.get("document_state") or "",
                "program": m.get("document_program") or "",
                "authority_level": m.get("document_authority_level") or "",
            },
        ))

    if len(sources) == 1:
        text = f"Found **{sources[0].document_name}**. Click Download to get the PDF."
    else:
        names = ", ".join(s.document_name for s in sources[:3])
        text = (
            f"Found {len(sources)} matching documents: {names}. "
            "Pick the one you want and click Download."
        )

    return SkillEnvelope(
        text=text,
        signal="ok",
        sources=sources,
        extra={"fetch_intent": True, "match_count": len(sources)},
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
    )
)


__all__ = ["_run_fetch_document"]
