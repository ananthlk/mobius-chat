"""Doc assembly pipeline: confidence labels, neighbor expansion, Google fallback.

Post-reranker flow: assign confidence (Option D + B tiers), optionally expand with neighbors,
and apply Google search fallback when corpus confidence is low.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


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
    """Assign confidence to all chunks."""
    cfg = config or DocAssemblyConfig()
    return [assign_confidence(c, cfg) for c in chunks]


def filter_abstain(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only chunks that are not abstain (i.e. send to LLM)."""
    return [c for c in chunks if c.get("confidence_label") != "abstain"]


def best_score(chunks: list[dict[str, Any]]) -> float:
    """Return the highest rerank_score (or match_score) among chunks, or 0."""
    if not chunks:
        return 0.0
    best = 0.0
    for c in chunks:
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
        else:
            results = out.get("results") or out.get("items") or []
        out_list: list[dict[str, Any]] = []
        for r in results[:max_results]:
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
    filtered = filter_abstain(chunks_with_conf)

    if best >= cfg.confidence_process_confident_min:
        _emit("Corpus confidence sufficient; using retrieved docs only.")
        return (filtered, RETRIEVAL_SIGNAL_CORPUS_ONLY)

    if best >= cfg.google_fallback_low_match_min:
        _emit("Adding external search to complement corpus...")
        google_results = google_search_via_skills_api(question)
        if google_results:
            return (filtered + google_results, RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE)
        return (filtered, RETRIEVAL_SIGNAL_CORPUS_PLUS_GOOGLE)

    _emit("Low corpus confidence; using external search.")
    google_results = google_search_via_skills_api(question)
    if google_results:
        return (google_results, RETRIEVAL_SIGNAL_GOOGLE_ONLY)
    if filtered:
        return (filtered, RETRIEVAL_SIGNAL_GOOGLE_ONLY)
    return ([], RETRIEVAL_SIGNAL_NO_SOURCES)


def _fetch_sibling_paragraphs(
    database_url: str,
    document_id: str,
    paragraph_index: int,
    chunk_id: Any,
    window: int = 2,
) -> list[dict[str, Any]]:
    """Fetch +/- window paragraphs (siblings) from same document. Excludes chunk_id."""
    if not database_url or not document_id:
        return []
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(database_url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        lo = max(0, (paragraph_index or 0) - window)
        hi = (paragraph_index or 0) + window
        cur.execute(
            """SELECT id, document_id, text, page_number, paragraph_index, document_display_name, document_filename
               FROM published_rag_metadata
               WHERE document_id::text = %s AND paragraph_index BETWEEN %s AND %s AND id::text != %s
               ORDER BY paragraph_index""",
            (str(document_id), lo, hi, str(chunk_id) if chunk_id else ""),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [
            {
                "id": r.get("id"),
                "text": r.get("text") or "",
                "document_id": str(r["document_id"]) if r.get("document_id") else None,
                "document_name": (r.get("document_display_name") or r.get("document_filename") or "document"),
                "page_number": r.get("page_number"),
                "paragraph_index": r.get("paragraph_index"),
                "source_type": "chunk",
                "match_score": None,
                "confidence": None,
                "is_neighbor": True,
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("Failed to fetch sibling paragraphs: %s", e)
        return []


def assemble_with_neighbors(
    chunks: list[dict[str, Any]],
    database_url: str,
    *,
    config: DocAssemblyConfig | None = None,
    window: int = 2,
) -> list[dict[str, Any]]:
    """Expand each chunk with +/- window sibling paragraphs from same document.

    Neighbors are appended after each core chunk. No JPD overlap filter in this version
    (can be added when line_tags/doc_tags available).
    """
    cfg = config or DocAssemblyConfig()
    seen_ids: set[str] = set()
    out: list[dict[str, Any]] = []
    for c in chunks:
        cid = str(c.get("id") or "")
        if cid and cid in seen_ids:
            continue
        seen_ids.add(cid)
        out.append(dict(c))
        doc_id = c.get("document_id")
        para_idx = c.get("paragraph_index")
        if database_url and doc_id is not None:
            siblings = _fetch_sibling_paragraphs(
                database_url,
                str(doc_id),
                para_idx if para_idx is not None else 0,
                c.get("id"),
                window=window,
            )
            for s in siblings:
                sid = str(s.get("id") or "")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    out.append(s)
    return out


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
    filtered = filter_abstain(chunks_with_conf)
    signal = RETRIEVAL_SIGNAL_CORPUS_ONLY if filtered else RETRIEVAL_SIGNAL_NO_SOURCES
    return (filtered, signal)
