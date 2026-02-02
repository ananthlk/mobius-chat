"""Published RAG search: Vertex AI Vector Search (1536 dims) + Postgres published_rag_metadata.
Flow: embed query -> find_neighbors with filters -> fetch metadata by id.
Two retrieval modes:
- Factual: top-k by similarity + confidence_min (no source_type filter).
- Hierarchical: ask Vertex for neighbors with source_type in [policy, section, chunk, hierarchical] via filter
  (mart may use "hierarchical" vs "fact"); if index returns 0, fall back to fetch-then-sort in code.
"""
import logging
from typing import Any, Callable, List

from app.trace_log import trace_entered

logger = logging.getLogger(__name__)

# Source-type hierarchy for canonical/hierarchical retrieval (lower index = higher in hierarchy).
# Mart may use "hierarchical" and "fact"; we also support policy/section/chunk if present.
SOURCE_TYPE_ORDER = ("policy", "section", "chunk", "hierarchical", "fact")

# Namespace name used in Vertex index for filtering by source_type (sync must expose this as a restrict)
VERTEX_SOURCE_TYPE_NAMESPACE = "source_type"


def _emit(emitter: Callable[[str], None] | None, chunk: str) -> None:
    if emitter and chunk.strip():
        emitter(chunk.strip())


def _hierarchy_rank(source_type: str | None) -> int:
    """Lower rank = higher in hierarchy (prefer policy > section > chunk > hierarchical > fact)."""
    st = (source_type or "chunk").strip().lower()
    for i, t in enumerate(SOURCE_TYPE_ORDER):
        if st == t or st.startswith(t):
            return i
    return len(SOURCE_TYPE_ORDER)


def search_published_rag(
    question: str,
    k: int = 10,
    confidence_min: float | None = None,
    source_type_allow: List[str] | None = None,
    emitter: Callable[[str], None] | None = None,
) -> List[dict[str, Any]]:
    """Search published RAG: embed question (1536), query Vertex with filters, fetch metadata from Postgres by id.
    If confidence_min is set, only return chunks with confidence >= confidence_min (after fetching k).
    If source_type_allow is set, restrict Vertex results to those source_type values (index must expose source_type namespace).
    Returns list of dicts with keys: text, document_id, document_name, page_number, source_type (same shape as legacy RAG).
    """
    trace_entered("services.published_rag_search.search_published_rag", k=k)
    from app.chat_config import get_chat_config
    from app.services.embedding_provider import get_query_embedding

    cfg = get_chat_config()
    rag = cfg.rag
    logger.info(
        "[RAG search] config: vertex_index_endpoint_id=%r vertex_deployed_index_id=%r (len=%s) database_url_set=%s",
        rag.vertex_index_endpoint_id,
        rag.vertex_deployed_index_id,
        len(rag.vertex_deployed_index_id or ""),
        bool(rag.database_url),
    )
    if not rag.vertex_index_endpoint_id or not rag.vertex_deployed_index_id or not rag.database_url:
        logger.warning("Published RAG: vertex_index_endpoint_id, vertex_deployed_index_id, or database_url not set")
        return []

    try:
        _emit(emitter, "Getting your question ready to search...")
        query_embedding = get_query_embedding(question)
    except Exception as e:
        logger.exception("Published RAG embedding failed: %s", e)
        return []

    # Build Vertex filter from config (Namespace uses allow_tokens / deny_tokens)
    filters: List[Any] = []
    try:
        from google.cloud.aiplatform.matching_engine.matching_engine_index_endpoint import Namespace
        if rag.filter_payer:
            filters.append(Namespace(name="document_payer", allow_tokens=[rag.filter_payer], deny_tokens=[]))
        if rag.filter_state:
            filters.append(Namespace(name="document_state", allow_tokens=[rag.filter_state], deny_tokens=[]))
        if rag.filter_program:
            filters.append(Namespace(name="document_program", allow_tokens=[rag.filter_program], deny_tokens=[]))
        if rag.filter_authority_level:
            filters.append(Namespace(name="document_authority_level", allow_tokens=[rag.filter_authority_level], deny_tokens=[]))
        # Explicit hierarchical: ask Vertex for chunks with source_type in [policy, section, chunk, hierarchical]
        if source_type_allow:
            filters.append(Namespace(name=VERTEX_SOURCE_TYPE_NAMESPACE, allow_tokens=source_type_allow, deny_tokens=[]))
    except ImportError as e:
        logger.warning("Vertex Namespace not available: %s", e)

    if filters:
        logger.info(
            "RAG filters applied: payer=%s state=%s program=%s authority_level=%s source_type_allow=%s",
            rag.filter_payer or "(none)", rag.filter_state or "(none)",
            rag.filter_program or "(none)", rag.filter_authority_level or "(none)",
            source_type_allow or "(none)",
        )

    try:
        from google.cloud import aiplatform
        from google.api_core.exceptions import ServiceUnavailable, NotFound
        aiplatform.init(project=cfg.llm.vertex_project_id, location=cfg.llm.vertex_location or "us-central1")
        endpoint = aiplatform.MatchingEngineIndexEndpoint(index_endpoint_name=rag.vertex_index_endpoint_id)
        _emit(emitter, "Searching our materials...")
        deployed_id = (rag.vertex_deployed_index_id or "").strip()
        # Vertex API requires the deployed index ID (e.g. endpoint_mobius_chat_publi_*), not the display name
        if deployed_id in ("Endpoint_mobius_chat_published_rag", "mobius_chat_published_rag"):
            deployed_id = "endpoint_mobius_chat_publi_1769989702095"
            logger.info("[RAG search] normalized display name → deployed_index_id=%r", deployed_id)
        print(f"[RAG find_neighbors] deployed_index_id={deployed_id!r}", flush=True)
        logger.info(
            "[RAG search] find_neighbors: deployed_index_id=%r",
            deployed_id,
        )
        response = endpoint.find_neighbors(
            deployed_index_id=deployed_id,
            queries=[query_embedding],
            num_neighbors=k,
            filter=filters if filters else None,
        )
        # response is List[List[MatchNeighbor]]; one query -> response[0]; neighbors have id and optionally distance
        neighbor_list = response[0] if response else []
        ids = [n.id for n in neighbor_list if n.id]
        id_to_distance: dict[str, float] = {}
        for n in neighbor_list:
            nid = getattr(n, "id", None)
            if nid is not None:
                dist = getattr(n, "distance", None)
                if dist is not None:
                    try:
                        id_to_distance[str(nid)] = float(dist)
                    except (TypeError, ValueError):
                        pass
        logger.info("Vertex find_neighbors returned %d id(s)", len(ids))
    except NotFound as e:
        logger.exception(
            "Vertex Vector Search 404 (index/deployed index not found): %s. "
            "Check VERTEX_INDEX_ENDPOINT_ID and VERTEX_DEPLOYED_INDEX_ID: the deployed index id must match exactly what is shown in Vertex AI Console (Index Endpoints → your endpoint → Deployed indexes). It may differ from the display name.",
            e,
        )
        return []
    except ServiceUnavailable as e:
        logger.exception(
            "Vertex Vector Search unreachable (503): %s. "
            "If the index endpoint is private (VPC), run the worker from the same VPC (e.g. GCE, Cloud Run) or use a public endpoint.",
            e,
        )
        return []
    except Exception as e:
        logger.exception("Vertex find_neighbors failed: %s", e)
        return []

    if not ids:
        logger.warning(
            "RAG: Vertex returned 0 neighbors. Check: (1) Vertex index has datapoints; "
            "(2) If CHAT_RAG_FILTER_* are set, document_payer/state/program in index must match (e.g. Sunshine Health)."
        )
        return []

    # Fetch metadata from Postgres
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(rag.database_url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, document_id, source_type, text, page_number, document_display_name, document_filename FROM published_rag_metadata WHERE id::text = ANY(%s)",
            (ids,),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        logger.exception("Postgres published_rag_metadata fetch failed: %s", e)
        return []

    logger.info("Postgres published_rag_metadata returned %d row(s) for %d id(s)", len(rows), len(ids))
    if len(rows) < len(ids):
        logger.warning(
            "RAG: Some Vertex ids not found in Postgres (%d ids, %d rows). "
            "Ensure sync job wrote to the same CHAT_RAG_DATABASE_URL.",
            len(ids), len(rows),
        )

    # Preserve order by ids (Vertex returns by similarity); attach distance -> match_score, confidence
    id_to_row = {str(r["id"]): r for r in rows}
    ordered = []
    for id_ in ids:
        r = id_to_row.get(id_)
        if not r:
            continue
        doc_name = (r.get("document_display_name") or r.get("document_filename") or "document") if r else "document"
        distance = id_to_distance.get(str(id_))
        match_score = None
        confidence = None
        if distance is not None:
            # Cosine distance in [0, 2]; similarity 0-1 = 1 - distance/2
            try:
                d = float(distance)
                match_score = round(max(0.0, min(1.0, 1.0 - d / 2.0)), 4)
                confidence = match_score
            except (TypeError, ValueError):
                pass
        ordered.append({
            "id": r.get("id"),
            "text": r.get("text") or "",
            "document_id": str(r["document_id"]) if r.get("document_id") else None,
            "document_name": doc_name,
            "page_number": r.get("page_number"),
            "source_type": r.get("source_type") or "chunk",
            "distance": distance,
            "match_score": match_score,
            "confidence": confidence,
        })
    if confidence_min is not None:
        ordered = [c for c in ordered if (c.get("confidence") or 0.0) >= confidence_min]
    n = len(ordered)
    _emit(emitter, f"Found {n} relevant bit{'s' if n != 1 else ''}.")
    return ordered


def search_factual(
    question: str,
    k: int = 10,
    confidence_min: float | None = None,
    emitter: Callable[[str], None] | None = None,
) -> List[dict[str, Any]]:
    """Top-k by similarity (factual path). Optionally filter by confidence_min."""
    return search_published_rag(question, k=k, confidence_min=confidence_min, emitter=emitter)


def search_hierarchical(
    question: str,
    k: int = 3,
    emitter: Callable[[str], None] | None = None,
) -> List[dict[str, Any]]:
    """Hierarchical retrieval: ask Vertex for neighbors with source_type in [policy, section, chunk, hierarchical].
    Mart may use "hierarchical" vs "fact"; we request all non-fact types. If the index exposes source_type,
    we get k results from the vector DB. If 0 results, fall back to fetch more then sort in code.
    """
    # Exclude fact so vector DB returns only hierarchical types (policy, section, chunk, or hierarchical)
    hierarchical_types = [t for t in SOURCE_TYPE_ORDER if t != "fact"]
    chunks = search_published_rag(
        question,
        k=k,
        confidence_min=None,
        source_type_allow=hierarchical_types,
        emitter=emitter,
    )
    if chunks:
        # Optionally sort by hierarchy rank then confidence (in case we got mixed types)
        chunks = sorted(
            chunks,
            key=lambda c: (_hierarchy_rank(c.get("source_type")), -(c.get("confidence") or 0.0)),
        )[:k]
        return chunks
    # Fallback: index may not have source_type namespace; fetch more and sort in code
    logger.info(
        "RAG: hierarchical filter returned 0 results; index may not have '%s' namespace. "
        "Falling back to fetch-then-sort. Populate source_type in Vertex index for true hierarchical retrieval.",
        VERTEX_SOURCE_TYPE_NAMESPACE,
    )
    fetch_k = max(20, 2 * k)
    chunks = search_published_rag(question, k=fetch_k, confidence_min=None, emitter=emitter)
    if not chunks:
        return []
    chunks_sorted = sorted(
        chunks,
        key=lambda c: (_hierarchy_rank(c.get("source_type")), -(c.get("confidence") or 0.0)),
    )
    return chunks_sorted[:k]


def retrieve_with_blend(
    question: str,
    n_hierarchical: int = 0,
    n_factual: int = 0,
    confidence_min: float | None = None,
    emitter: Callable[[str], None] | None = None,
) -> List[dict[str, Any]]:
    """Run hierarchical and/or factual retrieval per blend; merge and dedupe by chunk id."""
    trace_entered("services.published_rag_search.retrieve_with_blend", n_hierarchical=n_hierarchical, n_factual=n_factual)
    combined: List[dict[str, Any]] = []
    if n_hierarchical > 0:
        _emit(emitter, "Searching for high-level (hierarchical) materials...")
        H = search_hierarchical(question, k=n_hierarchical, emitter=emitter)
        combined.extend(H)
    if n_factual > 0:
        _emit(emitter, "Searching for specific facts...")
        F = search_factual(question, k=n_factual, confidence_min=confidence_min, emitter=emitter)
        combined.extend(F)
    if not combined:
        return []
    # Dedupe by chunk id (keep first occurrence: hierarchical then factual)
    seen: set[Any] = set()
    out: List[dict[str, Any]] = []
    for c in combined:
        cid = c.get("id")
        if cid is None:
            cid = (c.get("document_id"), c.get("page_number"), (c.get("text") or "")[:80])
        if cid not in seen:
            seen.add(cid)
            out.append(c)
    n = len(out)
    _emit(emitter, f"Using {n} result{'s' if n != 1 else ''} to answer this part.")
    # Warn when all chunks share same source_type (hierarchical retrieval had no diversity to prefer)
    if n > 1:
        stypes = {c.get("source_type") or "chunk" for c in out}
        if len(stypes) == 1:
            logger.info(
                "RAG: all %d retrieved chunks have source_type=%s; hierarchy had no effect. "
                "For canonical questions to prefer policy/section/hierarchical, populate source_type (policy/section/chunk/hierarchical/fact) in published_rag_metadata.",
                n, next(iter(stypes)),
            )
    return out
