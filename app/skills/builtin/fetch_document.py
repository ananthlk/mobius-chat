"""Builtin skill: ``fetch_document`` — resolve a corpus document by
name / filename / policy ID and return a download link.

Distinct from ``rag`` / ``search_corpus`` (which answers a question using
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
import urllib.parse
import urllib.request
import uuid
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


# Task #106 (2026-08-16, Ananth, directly): "these models are much better...
# if a doc is small enough to send let's add those as attachments... we
# don't have to build parsing... just need to restrict based on the size
# of the document as proxy for token limits." 8MB keeps real margin under
# Vertex's ~20MB inline-request ceiling (prompt text + safety headroom for
# a bigger document than expected) while covering the vast majority of
# real policy/handbook PDFs, which run low-single-digit MB.
_ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024
_ATTACHMENT_FETCH_TIMEOUT_S = 6  # fail-fast: attachment is best-effort, must NEVER stack toward the turn's 90s deadline on a slow RAG (2026-08-17)


def _guess_mime_type(filename: str) -> str:
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return {
        "pdf": "application/pdf",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain",
    }.get(ext, "application/pdf")  # corpus documents are overwhelmingly PDF


def _maybe_fetch_attachment(document_id: str, filename: str) -> dict[str, Any] | None:
    """Download the document's actual bytes for a native LLM attachment,
    when it's small enough. Reuses the same download endpoints already
    built for the FE's download-card button (``_download_url``/
    ``_fallback_download_url``) -- no new fetch mechanism, no parsing, the
    model reads the raw file directly (Ananth's explicit ruling: lean on
    the model's own document understanding instead of hand-rolled
    extraction). Any failure (404, timeout, oversized, network error)
    degrades silently to today's exact behavior -- a download card with no
    attachment -- never breaks the turn over a best-effort optimization.
    """
    for url in (_download_url(document_id), _fallback_download_url(document_id)):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=_ATTACHMENT_FETCH_TIMEOUT_S) as resp:
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > _ATTACHMENT_MAX_BYTES:
                    logger.info(
                        "fetch_document: skipping attachment for %s (%s bytes > %s cap)",
                        document_id, content_length, _ATTACHMENT_MAX_BYTES,
                    )
                    return None
                data = resp.read(_ATTACHMENT_MAX_BYTES + 1)
                if len(data) > _ATTACHMENT_MAX_BYTES:
                    logger.info(
                        "fetch_document: skipping attachment for %s (exceeded %s cap while streaming)",
                        document_id, _ATTACHMENT_MAX_BYTES,
                    )
                    return None
                if not data:
                    continue
                content_type = resp.headers.get("Content-Type") or _guess_mime_type(filename)
                import base64
                return {
                    "mime_type": content_type.split(";")[0].strip() or _guess_mime_type(filename),
                    "data_b64": base64.b64encode(data).decode("ascii"),
                    "filename": filename or f"{document_id}.pdf",
                }
        except Exception as e:
            logger.debug("fetch_document: attachment fetch failed for %s via %s: %s", document_id, url, e)
            continue
    return None


# ── Page-range extraction + doc size (§4) ───────────────────────────

_MAX_PAGE_SPAN = 40            # cap pages per request (bounds fetch + attachment)
_MAX_PAGES_TEXT_CHARS = 200_000


def _parse_page_spec(spec: str) -> list[int]:
    """Parse a page spec into a sorted, de-duped, capped page list.

    Accepts ``"20-24"`` (range), ``"20,80"`` (list), ``"20-24,80"``
    (mixed), or ``"20"`` (single). Returns [] on anything unparseable so
    a bad spec degrades to no-page-range rather than an error.
    """
    if not spec or not isinstance(spec, str):
        return []
    pages: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                lo, hi = int(a), int(b)
                if lo <= hi:
                    pages.update(range(lo, hi + 1))
        elif part.isdigit():
            pages.add(int(part))
    return sorted(p for p in pages if p >= 1)[:_MAX_PAGE_SPAN]


def _document_page_count(document_id: str) -> int | None:
    """Total page count from RAG ``/documents/{id}/status`` (cheap — no
    full page-text fetch). Best-effort; None on any failure."""
    base = _rag_api_base()
    if not base or not document_id:
        return None
    try:
        req = urllib.request.Request(
            f"{base}/documents/{urllib.parse.quote(document_id)}/status",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:  # page_count: fail-fast
            data = json.loads(resp.read().decode("utf-8")) or {}
    except Exception as e:
        logger.debug("fetch_document: page_count fetch failed for %s: %s", document_id, e)
        return None
    summary = data.get("pages_summary") or {}
    for v in (summary.get("total"), data.get("pages_extracted")):
        if isinstance(v, int) and v > 0:
            return v
    return None


def _fetch_pages_attachment(document_id: str, pages: list[int], base_name: str) -> dict[str, Any] | None:
    """Fetch specific pages' text from RAG and return them as a
    text/markdown attachment (same shape _maybe_fetch_attachment uses, so
    react_loop threads it identically — no new mechanism). This is the
    large-document answer: a 261-page handbook that exceeds the whole-file
    attach cap can still hand the model exactly the pages it asked for."""
    base = _rag_api_base()
    if not base or not pages:
        return None
    want = set(pages)
    try:
        req = urllib.request.Request(
            f"{base}/documents/{urllib.parse.quote(document_id)}/pages",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # page-range fetch: fail-fast
            payload = json.loads(resp.read().decode("utf-8")) or {}
    except Exception as e:
        logger.debug("fetch_document: page-range fetch failed for %s: %s", document_id, e)
        return None
    parts: list[str] = []
    total = 0
    for p in payload.get("pages") or []:
        pn = p.get("page_number")
        if pn not in want:
            continue
        text = (p.get("text_markdown") or p.get("text") or "").strip()
        if not text:
            continue
        block = f"[page {pn}]\n{text}"
        parts.append(block)
        total += len(block)
        if total >= _MAX_PAGES_TEXT_CHARS:
            break
    if not parts:
        return None
    import base64
    body = ("\n\n".join(parts))[:_MAX_PAGES_TEXT_CHARS]
    spec = f"{pages[0]}-{pages[-1]}" if len(pages) > 1 else str(pages[0])
    stem = (base_name.rsplit(".", 1)[0] if base_name else document_id) or document_id
    return {
        "mime_type": "text/markdown",
        "data_b64": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "filename": f"{stem}_pages_{spec}.md",
    }


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
    """Lowercase, alphanumeric tokens, drop stopwords.

    Dotted tokens are kept whole ("FL.UM.87" must match a policy-ID
    query exactly) AND emitted as their dot-parts — otherwise a
    filename like ``Provider_Manual.pdf`` tokenizes to ``manual.pdf``
    and never matches the query word "manual"."""
    out: dict[str, None] = {}  # ordered de-dupe
    for tok in re.findall(r"[A-Za-z0-9.]+", (s or "").lower()):
        tok = tok.strip(".")
        candidates = [tok]
        if "." in tok:
            candidates.extend(tok.split("."))
        for c in candidates:
            if c and c not in _STOPWORDS and len(c) > 1:
                out[c] = None
    return list(out)


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


def _normalize_db_rows(result: Any) -> list[dict[str, Any]]:
    """Normalize db_query's two return shapes to list[dict].

    db_query returns either ``{"rows": [{...}, ...]}`` (db-agent / dict
    rows) or ``{"columns": [...], "rows": [[...], ...]}`` (direct
    psycopg2 fallback). Shared by _fetch_candidates and _resolve_by_id.
    """
    if isinstance(result, dict) and result.get("error"):
        logger.warning("fetch_document: db_query error %s", result.get("error"))
        return []
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


_METADATA_COLUMNS = """
    document_id::text AS document_id,
    document_display_name,
    document_filename,
    document_payer,
    document_state,
    document_program,
    document_authority_level,
    updated_at
"""


def _looks_like_uuid(value: str) -> bool:
    """True when ``value`` is a well-formed UUID (the shape of our PK).

    Used to tell a genuine ``document_id`` apart from a filename/title a
    caller misrouted into that field — nobody outside the DB knows the
    UUID, so a non-UUID value is a name, not an id.
    """
    try:
        uuid.UUID((value or "").strip())
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _resolve_by_id(document_id: str) -> dict[str, Any] | None:
    """Deterministic single-document resolve by primary key (§8.1).

    When a caller already knows WHICH document it wants — react after a
    disambiguation round, or Fact Store with a ``source_ref.doc_id`` —
    there's no reason to re-run fuzzy matching. Resolve the exact row by
    PK and return its metadata in the same shape ``_fetch_candidates``
    yields, or ``None`` when the id is malformed or unknown (the caller
    turns that into a clean ``no_sources`` envelope — never an
    exception, per §2's contract).

    The UUID format is validated in-process first so a malformed id is a
    quiet no-match rather than a Postgres ``invalid input syntax for
    type uuid`` error round-trip.
    """
    doc_id = (document_id or "").strip()
    if not doc_id:
        return None
    try:
        uuid.UUID(doc_id)
    except (ValueError, AttributeError, TypeError):
        return None

    from app.db_client import db_query

    sql = f"""
        SELECT DISTINCT ON (document_id)
            {_METADATA_COLUMNS}
        FROM published_rag_metadata
        WHERE document_id = %(doc_id)s
        ORDER BY document_id, updated_at DESC
    """
    try:
        rows = _normalize_db_rows(db_query(sql, "chat", params={"doc_id": doc_id}))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("fetch_document: resolve_by_id query failed for %s: %s", doc_id, exc)
        return None
    return rows[0] if rows else None


_DOCGRAIN_VIEW = "published_rag_documents"
# Recent-slice window for the freshness path. MUST exceed the MV refresh
# cadence (app.services.docgrain_refresh, ~10 min) so a doc published
# since the last refresh is always caught here until the MV catches up.
_RECENT_WINDOW = "30 minutes"


def _fetch_candidates(query: str, *, limit: int = 30) -> list[dict[str, Any]]:
    """Pull document candidates from Postgres metadata.

    The coarse token filter MUST live in SQL: an unfiltered scan silently
    ranks an arbitrary subset (this is how "Sunshine provider manual"
    missed Sunshine's Provider_Manual.pdf). Any-token ILIKE over
    name/filename/payer keeps recall high; the Python ranking above stays
    the precision layer.

    Perf (2026-08-17): the primary source is the DOC-GRAIN materialized
    view ``published_rag_documents`` (~9210 rows, one per document,
    migration 059) rather than the chunk-grain base table (~1.95M rows).
    The trigram index (058) + ≥3-char filter only helps when tokens are
    RARE. A real document TITLE is common words — "150"/"services"/"policy"
    each match 100k+ chunk rows — so on the base table the ILIKE ANY +
    DISTINCT ON dedup cost 7,302 ms (measured, 59G-4.150 title). The same
    query over 9k doc-grain rows is ~220 ms (~30x).

    Freshness: the MV is a cache refreshed every ~10 min, so a
    just-published doc isn't in it yet. We therefore ALSO query a small
    "recent" slice of the base table (rows with updated_at inside
    ``_RECENT_WINDOW``), which the updated_at index serves in ~0.5 ms, and
    MERGE it with the MV result (dedup by document_id). This closes the
    blind spot where a brand-new doc's tokens overlap older docs already
    in the MV — without it, the MV would return the old docs and the new
    one would be invisible until the next refresh.

    If BOTH sources come back empty the tokens matched nothing recent and
    nothing in the 9k docs — i.e. they're selective — so a full base-table
    scan is cheap; we do it as a last resort (also covers the MV being
    absent when migration 059 hasn't run).
    """
    from app.db_client import db_query

    # Tokens are alphanumeric+dots only (see _tokenize), so no LIKE
    # metacharacter escaping is needed. ≥3 chars only — see docstring.
    patterns = [f"%{t}%" for t in _tokenize(query) if len(t) >= 3][:8]
    ilike = (
        "(document_display_name ILIKE ANY(%(patterns)s)"
        " OR document_filename ILIKE ANY(%(patterns)s)"
        " OR document_payer ILIKE ANY(%(patterns)s))"
    )
    where = "document_id IS NOT NULL"
    params: dict[str, Any] = {}
    if patterns:
        where += " AND " + ilike
        params["patterns"] = patterns

    by_id: dict[Any, dict[str, Any]] = {}

    # Fast path: doc-grain MV (one row per document; no dedup needed).
    try:
        mv_sql = f"SELECT {_METADATA_COLUMNS} FROM {_DOCGRAIN_VIEW} WHERE {where}"
        for r in _normalize_db_rows(db_query(mv_sql, "chat", params=params)):
            by_id[r.get("document_id")] = r
    except Exception as exc:
        logger.warning(
            "fetch_document: doc-grain MV query failed, base table only: %s", exc
        )

    # Freshness path: docs updated since the last MV refresh. Indexed on
    # updated_at (~0.5 ms), so this is cheap even mid-ingestion. MV row
    # wins on a dup (same document, identical data).
    try:
        recent_sql = f"""
            SELECT DISTINCT ON (document_id)
                {_METADATA_COLUMNS}
            FROM published_rag_metadata
            WHERE updated_at > now() - %(recent_window)s::interval
              AND {where}
            ORDER BY document_id, updated_at DESC
        """
        rp = dict(params, recent_window=_RECENT_WINDOW)
        for r in _normalize_db_rows(db_query(recent_sql, "chat", params=rp)):
            by_id.setdefault(r.get("document_id"), r)
    except Exception as exc:
        logger.warning("fetch_document: recent-slice query failed (non-fatal): %s", exc)

    if by_id:
        return list(by_id.values())

    # Last resort: full base-table scan. Reached only when both sources
    # were empty (selective tokens) or the MV is absent — the cheap case,
    # not the 7s common-token case, which always returns from the MV.
    base_sql = f"""
        SELECT DISTINCT ON (document_id)
            {_METADATA_COLUMNS}
        FROM published_rag_metadata
        WHERE {where}
        ORDER BY document_id, updated_at DESC
    """
    return _normalize_db_rows(db_query(base_sql, "chat", params=params))


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


# ── Tier-0: this thread's uploaded files ────────────────────────────


_UPLOAD_INTENT_WORDS = ("upload", "uploaded", "attached", "attachment", "my file", "i sent")


def _thread_upload_matches(call: SkillCall, query: str) -> list[dict[str, Any]]:
    """Match the query against files uploaded on this thread.

    Reads ``active.uploaded_files[]`` (same records the ReAct upload
    fast-path uses). Uploads outrank the corpus ONLY when the ask is
    clearly about them: either the filename match is dominant (≥2
    tokens covering at least half the query) or the user said
    upload-ish words. A single stray token ("sunshine" matching an
    uploaded sunshine_claims.pdf) must NOT hijack a corpus ask like
    "Sunshine provider manual"."""
    active = call.active_context if isinstance(call.active_context, dict) else {}
    files = [
        f for f in (active.get("uploaded_files") or [])
        if isinstance(f, dict) and str(f.get("document_id") or "").strip()
    ]
    if not files:
        return []
    q = (query or "").lower()
    intent = any(w in q for w in _UPLOAD_INTENT_WORDS)
    qtokens = _tokenize(query)
    scored: list[tuple[int, dict[str, Any]]] = []
    for f in files:
        ftokens = set(_tokenize(str(f.get("filename") or "")))
        overlap = sum(1 for t in qtokens if t in ftokens)
        scored.append((overlap, f))
    scored.sort(key=lambda x: -x[0])

    strong = [f for o, f in scored if o >= 2 and o * 2 >= len(qtokens)]
    if strong:
        return strong[:3]
    if intent:
        named = [f for o, f in scored if o >= 1]
        if named:
            return named[:3]
        if len(files) == 1:
            return [files[0]]
    return []


def _upload_envelope(
    call: SkillCall, query: str, uploads: list[dict[str, Any]]
) -> SkillEnvelope:
    """Download cards for this thread's uploads — served by chat's own
    ownership-checked ``/chat/uploads/{id}/download`` (relative URL so
    it resolves against the chat origin)."""
    sources: list[SourceRef] = []
    download_docs: list[dict[str, Any]] = []
    for u in uploads:
        doc_id = str(u.get("document_id") or "").strip()
        fname = str(u.get("filename") or "upload")
        dl = f"/chat/uploads/{urllib.parse.quote(doc_id)}/download"
        sources.append(SourceRef(
            document_name=fname,
            document_id=doc_id,
            source_type="document",
            page_number=None,
            index=len(sources) + 1,
            text=fname,
            authority="thread_upload",
            extra={"fetch_intent": True, "download_url": dl, "filename": fname},
        ))
        download_docs.append({
            "document_id": doc_id,
            "title": fname,
            "download_url": dl,
            "filename": fname,
            "resolved_via": "thread_upload",
        })
    _attach_download_payload(call, download_docs, query)
    if len(download_docs) == 1:
        text = f"Here's your uploaded file **{download_docs[0]['title']}** — use the card below."
    else:
        text = f"Found {len(download_docs)} uploads on this thread matching that — pick one below."
    return SkillEnvelope(
        text=text,
        signal="ok",
        sources=sources,
        extra={
            "fetch_intent": True,
            "match_count": len(download_docs),
            "resolved_via": "thread_upload",
            "document_download_payload": {"documents": download_docs, "query": query},
        },
    )


# ── Tier-3 fallback: curator web-source registry ────────────────────


_DOC_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx")


def _rag_service_base() -> str:
    return (
        os.environ.get("RAG_API_URL") or os.environ.get("RAG_API_BASE") or ""
    ).strip().rstrip("/")


def _web_registry_resolve(query: str, *, limit: int = 3) -> list[dict[str, Any]]:
    """Resolve against the curator's sitemap-fed URL registry
    (``discovered_sources`` via RAG ``GET /sources/search``).

    Covers documents Mobius knows exist on payer/agency sites but
    hasn't ingested — the same registry behind the planner's
    ``lookup_authoritative_sources`` tool, but returning download
    cards instead of prose. Rows are ts_rank-ordered on the RAG side;
    we keep only document-shaped URLs (pdf/office extensions or
    content type)."""
    base = _rag_service_base()
    if not base:
        return []
    params = urllib.parse.urlencode(
        {"q": query, "only_reachable": "true", "limit": 15}
    )
    req = urllib.request.Request(f"{base}/sources/search?{params}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        rows = json.loads(resp.read().decode("utf-8")) or []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        url = (r.get("url") or "").strip()
        if not url:
            continue
        parsed = urllib.parse.urlparse(url)
        # Registry rows include internal gs:// paths (already-ingested
        # bucket objects — tiers 1/2 own those). Only http(s) URLs are
        # browser-downloadable.
        if parsed.scheme not in ("http", "https"):
            continue
        basename = urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]).strip()
        content_type = (r.get("content_type") or "").lower()
        looks_like_doc = basename.lower().endswith(_DOC_EXTENSIONS) or any(
            marker in content_type
            for marker in ("pdf", "msword", "officedocument")
        )
        if not looks_like_doc:
            continue
        title = (
            re.sub(r"[-_]+", " ", basename.rsplit(".", 1)[0]).strip().title()
            or basename
            or url
        )
        out.append({
            "web_url": url,
            "host": parsed.netloc,
            "filename": basename,
            "title": title,
            "payer": r.get("payer") or "",
            "state": r.get("state") or "",
            "authority_level": r.get("effective_authority_level") or "",
            "ingested": bool(r.get("ingested")),
        })
        if len(out) >= limit:
            break
    return out


def _web_registry_envelope(
    call: SkillCall, query: str, web_docs: list[dict[str, Any]]
) -> SkillEnvelope:
    """Build the envelope + download cards for registry-resolved web docs.

    Cards link straight to the source site (no proxy). The frontend's
    fetch will typically CORS-fail on foreign hosts and fall back to a
    new-tab navigation download — which is the intended v1 behavior."""
    sources: list[SourceRef] = []
    download_docs: list[dict[str, Any]] = []
    for w in web_docs:
        # Primary: chat's same-origin download proxy (clean blob save,
        # real filename, audit-logged). Fallback: the direct source URL
        # for when the proxy declines (size cap, registry blip).
        proxy_url = "/chat/download-proxy?url=" + urllib.parse.quote(w["web_url"], safe="")
        sources.append(SourceRef(
            document_name=w["title"],
            document_id=None,
            source_type="web",
            page_number=None,
            index=len(sources) + 1,
            text=w["web_url"],
            url=w["web_url"],
            authority="web_registry",
            extra={
                "fetch_intent": True,
                "download_url": proxy_url,
                "fallback_download_url": w["web_url"],
                "filename": w["filename"],
                "host": w["host"],
                "payer": w["payer"],
                "state": w["state"],
                "authority_level": w["authority_level"],
            },
        ))
        download_docs.append({
            "document_id": f"web:{w['web_url']}",
            "title": w["title"],
            "download_url": proxy_url,
            "fallback_download_url": w["web_url"],
            "filename": w["filename"],
            "host": w["host"],
            "payer": w["payer"],
            "state": w["state"],
            "authority_level": w["authority_level"],
            "resolved_via": "web_registry",
        })

    _attach_download_payload(call, download_docs, query)

    hosts = ", ".join(sorted({w["host"] for w in web_docs}))
    if len(web_docs) == 1:
        text = (
            f"That document isn't in our corpus yet, but Mobius's source "
            f"registry knows it — **{web_docs[0]['title']}** on {hosts}. "
            "The download comes straight from the source site."
        )
    else:
        text = (
            f"Not in our corpus yet, but Mobius's source registry found "
            f"{len(web_docs)} matching documents on {hosts}. "
            "Downloads come straight from the source site."
        )
    return SkillEnvelope(
        text=text,
        signal="ok",
        sources=sources,
        extra={
            "fetch_intent": True,
            "match_count": len(download_docs),
            "resolved_via": "web_registry",
            "document_download_payload": {
                "documents": download_docs,
                "query": query,
            },
        },
    )


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
    document_id = (inputs.get("document_id") or "").strip()
    pages_spec = (inputs.get("pages") or "").strip()

    def _e(msg: str) -> None:
        if call.emitter and msg:
            call.emitter(msg)

    # §8.1 deterministic resolve-by-ID (2026-08-16). When the caller
    # already knows WHICH document it wants — react after a
    # disambiguation round, Fact Store with a source_ref.doc_id — skip
    # every fuzzy tier and resolve the exact row by primary key. Returns
    # exactly one card so the single-match content attachment (§1.3) is
    # GUARANTEED, not probabilistic. A free-text query, if also present,
    # is ignored in favor of the explicit id.
    if document_id and _looks_like_uuid(document_id):
        _e(f"◌ Resolving document by id: {document_id[:36]}…")
        row = _resolve_by_id(document_id)
        if not row:
            _e("  No document with that id")
            return SkillEnvelope(
                text="I couldn't find a document with that id in our materials.",
                signal="no_sources",
            )
        return _corpus_match_envelope(call, [row], query or document_id, "document_id", emit=_e, pages_spec=pages_spec)

    # Defensive misroute recovery (2026-08-17). A caller — commonly the
    # react planner — put a *filename or title* into `document_id` instead
    # of `query`. Nobody outside the DB knows the internal UUID PK, so a
    # human/planner sharing "Foo_Manual.pdf" always lands here. Rather than
    # dead-end at "no document with that id", reinterpret the value as a
    # name query and run the normal fuzzy pipeline (name-match is trigram-
    # accelerated, so it's cheap). Logged at INFO so we can measure how
    # often callers misroute and fix the manifest at the source.
    if document_id and not _looks_like_uuid(document_id):
        logger.info(
            "fetch_document: non-UUID document_id=%r reinterpreted as name query "
            "(misroute recovery)",
            document_id[:120],
        )
        if not query:
            query = document_id
        _e("  That's a name, not an id — searching by name…")
        document_id = ""  # fall through to the fuzzy tiers below

    if not query:
        return SkillEnvelope(
            text="No document query provided.",
            signal="no_sources",
        )

    # Tier 0: files uploaded on this thread ("send me back the file I
    # uploaded", "download my roster"). Checked first because thread
    # uploads are the most specific context we have.
    _e(f"◌ Looking up document: {query[:80]}…")
    try:
        uploads = _thread_upload_matches(call, query)
    except Exception as exc:
        logger.warning("fetch_document: thread-upload match failed: %s", exc)
        uploads = []
    if uploads:
        _e(f"✓ Found {len(uploads)} uploaded file(s) on this thread")
        return _upload_envelope(call, query, uploads)

    _e("  Searching document index…")
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
        _e("  No name match — trying corpus search…")
        try:
            matches = _merge_metadata(_corpus_search_resolve(query), candidates)
            resolved_via = "corpus_search"
        except Exception as exc:
            logger.warning("fetch_document: corpus_search fallback failed: %s", exc)
            matches = []
    if not matches:
        # Tier 3: the sitemap-fed web-source registry — docs Mobius
        # knows exist on payer/agency sites but hasn't ingested.
        _e("  Not in corpus — checking source registry…")
        try:
            web_docs = _web_registry_resolve(query)
        except Exception as exc:
            logger.warning("fetch_document: web registry fallback failed: %s", exc)
            web_docs = []
        if web_docs:
            _e(f"✓ Found {len(web_docs)} known source(s) in registry")
            return _web_registry_envelope(call, query, web_docs)
    if not matches:
        return SkillEnvelope(
            text=(
                "I don't see a document matching that in our materials or "
                "our source registry. If you have a copy, you can attach "
                "it to this thread."
            ),
            signal="no_sources",
        )

    return _corpus_match_envelope(call, matches, query, resolved_via, emit=_e, pages_spec=pages_spec)


_RESOLVED_VIA_LABEL = {
    "name_match": "name match",
    "corpus_search": "corpus search",
    "document_id": "id lookup",
}


def _corpus_match_envelope(
    call: SkillCall,
    matches: list[dict[str, Any]],
    query: str,
    resolved_via: str,
    *,
    emit: Any = None,
    pages_spec: str = "",
) -> SkillEnvelope:
    """Build the download-card envelope from ranked corpus-metadata rows.

    Shared by the fuzzy query path and the deterministic resolve-by-ID
    path (§8.1) so BOTH get identical SourceRef shape, the single-match
    content attachment (§1.3), and the ``golden=False`` opt-out (§1.2).
    resolve-by-ID passes a one-element ``matches`` list, which is what
    *guarantees* the single-match attachment fires rather than merely
    making it probable.
    """
    def _e(msg: str) -> None:
        cb = emit or call.emitter
        if cb and msg:
            cb(msg)

    # Top 3 — usually 1, but if the user said "Sunshine" we may have
    # both Provider Manual and Member Handbook. Multi-match renders as
    # a pick-list of download cards. (resolve-by-ID always yields 1.)
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

    if not sources:
        # Every candidate row lacked a document_id — treat as no-match
        # rather than emit an empty card.
        return SkillEnvelope(
            text="I couldn't resolve that to a downloadable document.",
            signal="no_sources",
        )

    _attach_download_payload(call, download_docs, query)

    _via = _RESOLVED_VIA_LABEL.get(resolved_via, resolved_via)
    _e(f"✓ Resolved {len(sources)} document(s) by {_via}")

    # Task #106 (2026-08-16, Ananth, directly): a single confident match --
    # the common case -- gets its actual bytes attached (size-permitting),
    # not just a filename, so the NEXT reasoning round can read the real
    # content natively instead of stopping at "here's a download link."
    # Multi-match stays a pick-list only; attaching 2-3 documents at once
    # to a single round isn't what this is for.
    attachment: dict[str, Any] | None = None
    if len(sources) == 1:
        doc_id = sources[0].document_id or ""
        fname = download_docs[0].get("filename") or ""
        requested_pages = _parse_page_spec(pages_spec)
        try:
            if requested_pages:
                # Targeted page-range: hand the model exactly the pages it
                # asked for (works for large docs the whole-file cap drops).
                attachment = _fetch_pages_attachment(doc_id, requested_pages, fname)
            else:
                attachment = _maybe_fetch_attachment(doc_id, fname)
        except Exception as e:
            logger.warning("fetch_document: attachment attempt raised for %s: %s", doc_id, e)
        # Surface page_count LAZILY — only when the whole file did NOT attach
        # and no pages were requested, i.e. exactly when the doc is large
        # enough that the planner needs the size to request a page range next
        # round. Small docs that attach fine don't pay a RAG /status
        # round-trip for a hint nothing will use (perf fix, 2026-08-17).
        if not requested_pages and attachment is None:
            try:
                page_count = _document_page_count(doc_id)
            except Exception:
                page_count = None
            if page_count:
                download_docs[0]["page_count"] = page_count
        if attachment and requested_pages:
            _e(f"✓ Attached {sources[0].document_name} pages {pages_spec} for this round")
        elif attachment:
            _e(f"✓ Attached {sources[0].document_name} ({len(attachment['data_b64']) * 3 // 4} bytes) for this round")
        text = f"Found **{sources[0].document_name}**. Use the card below to download it."
    else:
        names = ", ".join(s.document_name for s in sources[:3])
        text = (
            f"Found {len(sources)} possible matches: {names}. "
            "Pick the one you want from the cards below."
        )

    _extra: dict[str, Any] = {
        "fetch_intent": True,
        "match_count": len(sources),
        "resolved_via": resolved_via,
        "document_download_payload": {"documents": download_docs, "query": query},
        # Size signal so the planner can choose whole-file vs page-range next.
        **({"page_count": download_docs[0]["page_count"]} if len(sources) == 1 and download_docs and download_docs[0].get("page_count") else {}),
        **({"attached_pages": pages_spec} if attachment and _parse_page_spec(pages_spec) else {}),
        # Explicit opt-out (2026-08-16, live finding cid=a337ef54): resolving
        # WHICH document matches is not the same as having its content to
        # answer with -- see react_loop.py's golden-inference comment for
        # the full story. Without this, a successful resolve auto-finalized
        # the turn on a download link before react could read the attached
        # content (or fall back to rag/search_corpus) in a later round.
        "golden": False,
    }
    if attachment:
        _extra["attachment"] = attachment

    return SkillEnvelope(
        text=text,
        signal="ok",
        sources=sources,
        extra=_extra,
    )


# ── Registration ────────────────────────────────────────────────────


register(
    SkillSpec(
        name="fetch_document",
        description=(
            "Get you the actual SOURCE DOCUMENT behind an answer — the real "
            "file, not fragments retrieved from it. What I do:\n"
            "1. When there's ONE specific, identifiable document (a named "
            "manual, a policy ID, a filename, or a document_id you already "
            "hold) and it's small enough, I attach its actual CONTENT so you "
            "read the real source directly.\n"
            "2. When an answer is thin or vague because it needs the real "
            "underlying document, I locate that document — from the corpus, or "
            "from payer/agency sites Mobius knows about but hasn't ingested — "
            "and surface it (a download link for the user + the content for "
            "you).\n"
            "Reach for me when you need THE document: the user asked for the "
            "file itself ('send me…', 'download…'), or the question turns on one "
            "specific named document you should read in full. For a broad "
            "question answerable from passages spread across many documents, "
            "ordinary corpus retrieval is the better fit — I'm for when a "
            "single real source is the point, not an answer stitched from "
            "snippets.\n"
            "If the document is large, ask for just the pages you need "
            "(pages='20-24' or '20,80') instead of the whole file — the result "
            "reports the document's page_count so you can decide.\n"
            "Resolving it: a document_id is exact (no guessing); a name / policy "
            "ID matches by text; several matches come back as a short pick-list "
            "— call again with the chosen document_id to pin and attach it. "
            "(User-uploaded files have their own tool.)"
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
                "document_id": {
                    "type": "string",
                    "description": (
                        "A specific document's UUID. When provided, fuzzy "
                        "matching is skipped entirely and this exact "
                        "document is resolved (returns exactly one result). "
                        "Use for a follow-up after the user picks from a "
                        "multi-candidate result, or when you already hold "
                        "the id. Either query OR document_id is required."
                    ),
                },
                "pages": {
                    "type": "string",
                    "description": (
                        "Optional page range/list to attach instead of the "
                        "whole file — e.g. '20-24', '20,80', or '17'. Use this "
                        "for a large document (the result reports page_count) "
                        "when you only need a specific section: you get exactly "
                        "those pages' text, which works even when the whole "
                        "file is too big to attach. Applies to a single "
                        "resolved match (pair with document_id for precision)."
                    ),
                },
            },
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
