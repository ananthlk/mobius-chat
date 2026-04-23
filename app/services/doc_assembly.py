"""Doc assembly pipeline: confidence labels, neighbor expansion, Google fallback.

Post-reranker flow: assign confidence (Option D + B tiers), optionally expand with neighbors,
and apply Google search fallback when corpus confidence is low.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import os

logger = logging.getLogger(__name__)
_DEBUG_RAG = os.environ.get("DEBUG_RAG", "1").lower() in ("1", "true", "yes")


@dataclass
class DocAssemblyConfig:
    """Configurable thresholds for doc assembly (tunable via calibration)."""
    confidence_abstain_max: float = 0.5  # below this: abstain
    confidence_process_confident_min: float = 0.85  # above this: process_confident
    google_fallback_low_match_min: float = 0.5  # complement with Google when best in [0.5, 0.85)
    neighbor_jpd_overlap_min: float = 0.3  # min J/P/D overlap to include a neighbor


# Option B tiers; Option D = numeric + label
CONFIDENCE_TIERS = {
    "abstain": "Do not send",
    "process_with_caution": "Use but reconcile across docs",
    "process_confident": "Likely correct; verify no conflicts",
}


def _ensure_chunk_dict(c: Any) -> dict[str, Any]:
    """Normalize chunk to plain dict. Handles dict subclasses (e.g. Row) and list-of-(k,v)-pairs."""
    if isinstance(c, dict):
        return dict(c)
    if isinstance(c, (list, tuple)) and c and all(
        isinstance(x, (list, tuple)) and len(x) == 2 for x in c
    ):
        return dict(c)
    raise TypeError(
        f"Chunk must be dict or list of (k,v) pairs, got {type(c).__name__}: {repr(c)[:200]}"
    )


def assign_confidence(
    doc: dict[str, Any],
    config: DocAssemblyConfig | None = None,
) -> dict[str, Any]:
    """Assign confidence_label and llm_guidance from rerank_score or match_score.

    Uses rerank_score when present; else match_score or confidence (Vertex path).
    Returns doc with added keys: rerank_score, confidence_label, llm_guidance.
    """
    doc = _ensure_chunk_dict(doc)
    cfg = config or DocAssemblyConfig()
    score = doc.get("rerank_score")
    if score is None:
        score = doc.get("match_score") or doc.get("confidence") or 0.0
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0

    doc = dict(doc)
    doc["rerank_score"] = round(score, 4)

    if score < cfg.confidence_abstain_max:
        label = "abstain"
    elif score >= cfg.confidence_process_confident_min:
        label = "process_confident"
    else:
        label = "process_with_caution"

    doc["confidence_label"] = label
    doc["llm_guidance"] = CONFIDENCE_TIERS.get(label, "Use but reconcile across docs")
    return doc


def assign_confidence_batch(
    chunks: list[dict[str, Any]],
    config: DocAssemblyConfig | None = None,
) -> list[dict[str, Any]]:
    """Assign confidence to all chunks. Skips non-dict items."""
    cfg = config or DocAssemblyConfig()
    if _DEBUG_RAG and chunks:
        for i, c in enumerate(chunks[:3]):
            logger.info("[DEBUG_RAG doc_assembly] assign_confidence_batch chunk[%s] type=%s", i, type(c).__name__)
    return [assign_confidence(c, cfg) for c in chunks if isinstance(c, dict)]


def filter_abstain(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only chunks that are not abstain (i.e. send to LLM)."""
    return [c for c in chunks if isinstance(c, dict) and c.get("confidence_label") != "abstain"]


def best_score(chunks: list[dict[str, Any]]) -> float:
    """Return the highest rerank_score (or match_score) among chunks, or 0."""
    if not chunks:
        return 0.0
    best = 0.0
    for c in chunks:
        if not isinstance(c, dict):
            continue
        s = c.get("rerank_score") or c.get("match_score") or c.get("confidence") or 0.0
        try:
            best = max(best, float(s))
        except (TypeError, ValueError):
            pass
    return best


def google_search_via_skills_api(
    query: str,
    api_base: str | None = None,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """Call shared skills API for Google search. Returns list of snippet dicts.

    Expects env CHAT_SKILLS_GOOGLE_SEARCH_URL or passed api_base.
    Response shape: {"results": [{"snippet": str, "title": str, "url": str?}, ...]}
    """
    import os
    import urllib.parse
    import urllib.request

    base = api_base or os.environ.get("CHAT_SKILLS_GOOGLE_SEARCH_URL", "").strip()
    if not base:
        logger.warning("CHAT_SKILLS_GOOGLE_SEARCH_URL not set; skipping Google search fallback")
        return []

    sep = "&" if "?" in base else "?"
    url = base.rstrip("/") + sep + "q=" + urllib.parse.quote(query)

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode()
        import json
        out = json.loads(data)
        if isinstance(out, list):
            results = out
        elif isinstance(out, dict):
            results = out.get("results") or out.get("items") or []
        else:
            results = []
        out_list: list[dict[str, Any]] = []
        results_slice = results[:max_results] if isinstance(results, (list, tuple)) else []
        for r in results_slice:
            if isinstance(r, dict):
                snippet = r.get("snippet") or r.get("description") or r.get("text") or ""
                title = r.get("title") or ""
                url_val = r.get("url") or r.get("link") or ""
                if snippet or title:
                    out_list.append({
                        "text": (title + "\n" + snippet).strip() if title else snippet,
                        "document_name": title or url_val or "External",
                        "source_type": "external",
                        "confidence_label": "abstain",
                        "llm_guidance": "External source; use if helpful but retain/hedge; not from authoritative corpus.",
                        "rerank_score": 0.0,
                    })
        return out_list
    except Exception as e:
        logger.warning("Google search via skills API failed: %s", e)
        return []


# Retrieval signal for badge: what retrieval/assembler returned to LLM
RETRIEVAL_SIGNAL_CORPUS_ONLY = "corpus_only"
RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE = "corpus_plus_google"
RETRIEVAL_SIGNAL_GOOGLE_ONLY = "google_only"
RETRIEVAL_SIGNAL_NO_SOURCES = "no_sources"
RETRIEVAL_SIGNAL_ROSTER_COMPLETE = "roster_complete"
RETRIEVAL_SIGNAL_SYSTEM_CONTEXT = "system_context"
"""Answer grounded entirely in caller-supplied ``system_context`` (Round 0).

No RAG / web / tool retrieval ran. Sources list is intentionally empty
because the data was pre-verified by the caller (story layer node, skill
card, etc.). Dashboards that attribute cost/latency by signal should
treat this bucket as the lowest-cost path."""


def apply_google_fallback(
    chunks: list[dict[str, Any]],
    question: str,
    config: DocAssemblyConfig | None = None,
    emitter: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Apply Google fallback per plan. Returns (chunks, retrieval_signal).

    retrieval_signal: corpus_only | corpus_plus_google | google_only | no_sources
    """
    def _emit(msg: str) -> None:
        if emitter and msg.strip():
            emitter(msg.strip())

    cfg = config or DocAssemblyConfig()
    chunks_with_conf = assign_confidence_batch(chunks, cfg)
    best = best_score(chunks_with_conf)
    # Send all chunks (no abstain filter); complement with Google when confidence low
    all_chunks = chunks_with_conf

    if best >= cfg.confidence_process_confident_min:
        _emit("Corpus confidence sufficient; using retrieved docs only.")
        return (all_chunks, RETRIEVAL_SIGNAL_CORPUS_ONLY)

    if best >= cfg.google_fallback_low_match_min:
        _emit("Adding external search to complement corpus...")
        google_results = google_search_via_skills_api(question)
        if google_results:
            return (all_chunks + google_results, RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE)
        return (all_chunks, RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE)

    _emit("Low corpus confidence; using external search.")
    google_results = google_search_via_skills_api(question)
    if google_results:
        return (all_chunks + google_results, RETRIEVAL_SIGNAL_GOOGLE_ONLY)
    if all_chunks:
        return (all_chunks, RETRIEVAL_SIGNAL_GOOGLE_ONLY)
    return ([], RETRIEVAL_SIGNAL_NO_SOURCES)


# Phase 0.11: post-expansion caps to keep the citation list sane.
# Before this phase, a ~20-seed retrieval was ballooning to ~1,000+ chunks because
# ``paragraph_index`` is not globally monotonic in ``published_rag_metadata`` —
# it appears to reset per page, so ``paragraph_index BETWEEN N-2 AND N+2`` with no
# page constraint matched ~5 rows on EVERY page of the document. These caps are
# a defense-in-depth layer on top of the page-constrained sibling query below.
NEIGHBOR_TOTAL_CAP = 50          # hard ceiling on post-expansion chunk count
NEIGHBOR_PER_DOC_CAP = 8         # max chunks kept from any one document


def _fetch_sibling_paragraphs(
    database_url: str,
    document_id: str,
    paragraph_index: int,
    chunk_id: Any,
    window: int = 2,
    page_number: int | None = None,
    page_window: int = 1,
) -> list[dict[str, Any]]:
    """Fetch +/- ``window`` paragraphs (siblings) from same document, restricted to
    pages within ``page_window`` of the seed's page.

    DEPRECATED for pipeline use: prefer ``_fetch_sibling_paragraphs_batch`` which does
    ONE round-trip for many chunks. Kept for back-compat with single-chunk callers.
    """
    if not database_url or not document_id:
        return []
    return _fetch_sibling_paragraphs_batch(
        database_url,
        [{
            "document_id": document_id,
            "paragraph_index": paragraph_index,
            "page_number": page_number,
            "id": chunk_id,
        }],
        window=window,
        page_window=page_window,
    )


# Very large page range → effectively "no page constraint" for chunks without page_number.
_NO_PAGE_HI = 10_000_000


def _fetch_sibling_paragraphs_batch(
    database_url: str,
    chunks: list[dict[str, Any]],
    window: int = 2,
    page_window: int = 1,
) -> list[dict[str, Any]]:
    """Fetch siblings for MANY chunks in ONE Postgres round-trip (UNNEST).

    Previously did N separate queries (~1.4s each via Cloud SQL proxy → ~15-40s total
    per question). Now a single query JOINs the metadata table against an UNNEST'd
    virtual table of (doc_id, lo, hi, page_lo, page_hi, exclude_id) tuples.

    Page constraint (per Phase 0.11) is preserved: chunks with a page_number get
    ± ``page_window`` pages; chunks without one get a huge range that is effectively
    no constraint (so both paths are handled in one query).
    """
    if not database_url or not chunks:
        return []

    from app.db_client import db_query

    doc_ids: list[str] = []
    los: list[int] = []
    his: list[int] = []
    page_los: list[int] = []
    page_his: list[int] = []
    excludes: list[str] = []
    for c in chunks:
        doc_id = c.get("document_id")
        if doc_id is None:
            continue
        pi = c.get("paragraph_index")
        pi_int = int(pi) if pi is not None else 0
        doc_ids.append(str(doc_id))
        los.append(max(0, pi_int - window))
        his.append(pi_int + window)
        page = c.get("page_number")
        if isinstance(page, int):
            page_los.append(max(0, page - page_window))
            page_his.append(page + page_window)
        else:
            page_los.append(0)
            page_his.append(_NO_PAGE_HI)
        cid = c.get("id")
        excludes.append(str(cid) if cid is not None else "")
    if not doc_ids:
        return []

    sql = (
        "SELECT DISTINCT ON (m.id) "
        "       m.id, m.document_id, m.text, m.page_number, m.paragraph_index, "
        "       m.document_display_name, m.document_filename "
        "FROM published_rag_metadata m "
        "JOIN ( "
        "   SELECT UNNEST(:doc_ids::text[])   AS doc_id, "
        "          UNNEST(:los::int[])        AS lo, "
        "          UNNEST(:his::int[])        AS hi, "
        "          UNNEST(:page_los::int[])   AS page_lo, "
        "          UNNEST(:page_his::int[])   AS page_hi, "
        "          UNNEST(:excludes::text[])  AS exclude_id "
        ") r "
        "  ON m.document_id::text = r.doc_id "
        " AND m.paragraph_index BETWEEN r.lo AND r.hi "
        " AND m.page_number BETWEEN r.page_lo AND r.page_hi "
        " AND m.id::text <> COALESCE(NULLIF(r.exclude_id, ''), '00000000-0000-0000-0000-000000000000') "
        "ORDER BY m.id, m.page_number, m.paragraph_index"
    )
    params = {
        "doc_ids": doc_ids,
        "los": los, "his": his,
        "page_los": page_los, "page_his": page_his,
        "excludes": excludes,
    }

    # max_rows: caller expects caps applied downstream; request generous budget.
    result = db_query(sql, "chat", params=params, max_rows=5000)
    err = result.get("error") if isinstance(result, dict) else None
    if err:
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        logger.warning("Failed to fetch sibling paragraphs (batch): %s", msg)
        return []

    cols = result.get("columns") or []
    return [
        {
            "id": row.get("id"),
            "text": row.get("text") or "",
            "document_id": str(row["document_id"]) if row.get("document_id") else None,
            "document_name": (row.get("document_display_name") or row.get("document_filename") or "document"),
            "page_number": row.get("page_number"),
            "paragraph_index": row.get("paragraph_index"),
            "source_type": "chunk",
            "match_score": None,
            "confidence": None,
            "is_neighbor": True,
        }
        for row in (dict(zip(cols, r)) for r in (result.get("rows") or []))
    ]


def _apply_chunk_caps(
    chunks: list[dict[str, Any]],
    *,
    total_cap: int = NEIGHBOR_TOTAL_CAP,
    per_doc_cap: int = NEIGHBOR_PER_DOC_CAP,
) -> list[dict[str, Any]]:
    """Apply post-expansion caps. Keeps seeds (``is_neighbor`` not True) ahead of neighbors
    and orders within each doc by match_score desc, paragraph_index asc.

    Phase 0.11 defense-in-depth: even with the page-constrained neighbor query,
    a bad data shape (e.g. huge seeds from blend selection) could still oversize
    the citation list. These caps keep the UI citation count in double digits.
    """
    if not chunks:
        return []

    def _score(c: dict[str, Any]) -> float:
        try:
            s = c.get("match_score")
            if s is None:
                s = c.get("rerank_score") or c.get("confidence") or 0.0
            return float(s)
        except (TypeError, ValueError):
            return 0.0

    # Seeds first (preserving retrieval order), then neighbors sorted by score desc.
    seeds = [c for c in chunks if isinstance(c, dict) and not c.get("is_neighbor")]
    neighbors = [c for c in chunks if isinstance(c, dict) and c.get("is_neighbor")]
    neighbors.sort(key=_score, reverse=True)

    per_doc: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for c in seeds + neighbors:
        if len(out) >= total_cap:
            break
        doc_key = str(c.get("document_id") or c.get("document_name") or "_unknown")
        if per_doc.get(doc_key, 0) >= per_doc_cap:
            continue
        per_doc[doc_key] = per_doc.get(doc_key, 0) + 1
        out.append(c)
    return out


def assemble_with_neighbors(
    chunks: list[dict[str, Any]],
    database_url: str,
    *,
    config: DocAssemblyConfig | None = None,
    window: int = 2,
    page_window: int = 1,
    total_cap: int = NEIGHBOR_TOTAL_CAP,
    per_doc_cap: int = NEIGHBOR_PER_DOC_CAP,
) -> list[dict[str, Any]]:
    """Expand each chunk with sibling paragraphs within ``page_window`` pages
    and ``window`` paragraph indices, then apply per-doc and total caps.

    Performs ONE Postgres round-trip for all seeds (UNNEST batch) rather than
    N serial queries. Phase 0.11 page constraint is preserved per-seed inside
    the batched query.
    """
    cfg = config or DocAssemblyConfig()
    # Dedupe seeds by id, preserving order
    seen_ids: set[str] = set()
    seeds: list[dict[str, Any]] = []
    for c in chunks:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "")
        if cid and cid in seen_ids:
            continue
        seen_ids.add(cid)
        seeds.append(dict(c))

    out: list[dict[str, Any]] = list(seeds)
    if not database_url:
        return _apply_chunk_caps(out, total_cap=total_cap, per_doc_cap=per_doc_cap)

    # One batched query for all siblings across all seeds
    seeds_with_docs = [s for s in seeds if s.get("document_id") is not None]
    if seeds_with_docs:
        siblings = _fetch_sibling_paragraphs_batch(
            database_url,
            seeds_with_docs,
            window=window,
            page_window=page_window,
        )
        for s in siblings:
            sid = str(s.get("id") or "")
            if sid and sid not in seen_ids:
                seen_ids.add(sid)
                out.append(s)
    return _apply_chunk_caps(out, total_cap=total_cap, per_doc_cap=per_doc_cap)


def assemble_docs(
    chunks: list[dict[str, Any]],
    question: str,
    *,
    config: DocAssemblyConfig | None = None,
    apply_google: bool = True,
    expand_neighbors: bool = False,
    database_url: str | None = None,
    canonical_score: float | None = None,
    emitter: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Assemble final docs: assign confidence, optionally expand neighbors, optionally Google fallback.

    Returns (chunks, retrieval_signal) where retrieval_signal is corpus_only | corpus_plus_google | google_only | no_sources.
    """
    cfg = config or DocAssemblyConfig()
    if expand_neighbors and database_url:
        chunks = assemble_with_neighbors(chunks, database_url, config=cfg, window=2)
    chunks_with_conf = assign_confidence_batch(chunks, cfg)
    if apply_google:
        return apply_google_fallback(chunks_with_conf, question, cfg, emitter)
    signal = RETRIEVAL_SIGNAL_CORPUS_ONLY if chunks_with_conf else RETRIEVAL_SIGNAL_NO_SOURCES
    return (chunks_with_conf, signal)
