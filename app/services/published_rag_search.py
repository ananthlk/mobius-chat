"""Published RAG search: ChromaDB (default) or Vertex AI Vector Search + Postgres published_rag_metadata.
Flow: embed query -> vector search (Chroma or Vertex) with filters -> fetch metadata by id from Postgres.
Two retrieval modes:
- Factual: top-k by similarity + confidence_min (no source_type filter).
- Hierarchical: ask vector store for neighbors with source_type in [policy, section, chunk, hierarchical] via filter
  (mart may use "hierarchical" vs "fact"); if index returns 0, fall back to fetch-then-sort in code.
"""
from __future__ import annotations
import logging
from typing import Any, Callable, List

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


# ---------------------------------------------------------------------------
# ChromaDB vector search
# ---------------------------------------------------------------------------

_chroma_client = None
_chroma_collection = None


def _get_chroma_collection(persist_dir: str, collection_name: str):
    """Lazy-init ChromaDB persistent client and collection (cosine space)."""
    global _chroma_client, _chroma_collection
    if _chroma_collection is not None:
        return _chroma_collection
    import chromadb
    _chroma_client = chromadb.PersistentClient(path=persist_dir)
    _chroma_collection = _chroma_client.get_or_create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )
    return _chroma_collection


def _search_chroma(
    query_embedding: List[float],
    k: int,
    cfg,
    source_type_allow: List[str] | None = None,
) -> tuple[List[str], dict[str, float]]:
    """Query ChromaDB, return (ids, id_to_distance). Applies metadata filters."""
    rag = cfg.rag
    coll = _get_chroma_collection(rag.chroma_persist_dir, rag.chroma_collection)

    # Build Chroma where filter (AND logic via $and)
    conditions: List[dict] = []
    if rag.filter_payer:
        conditions.append({"document_payer": rag.filter_payer})
    if rag.filter_state:
        conditions.append({"document_state": rag.filter_state})
    if rag.filter_program:
        conditions.append({"document_program": rag.filter_program})
    if rag.filter_authority_level:
        conditions.append({"document_authority_level": rag.filter_authority_level})
    if source_type_allow:
        conditions.append({"source_type": {"$in": source_type_allow}})

    where = None
    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    if where:
        logger.info(
            "RAG Chroma filters: payer=%s state=%s program=%s authority_level=%s source_type_allow=%s",
            rag.filter_payer or "(none)", rag.filter_state or "(none)",
            rag.filter_program or "(none)", rag.filter_authority_level or "(none)",
            source_type_allow or "(none)",
        )

    result = coll.query(
        query_embeddings=[query_embedding],
        n_results=k,
        where=where,
        include=["distances"],
    )

    if not result or not result["ids"] or not result["ids"][0]:
        return [], {}

    ids = result["ids"][0]
    id_to_distance: dict[str, float] = {}
    if result.get("distances") and result["distances"][0]:
        for i, id_ in enumerate(ids):
            try:
                id_to_distance[str(id_)] = float(result["distances"][0][i])
            except (TypeError, ValueError, IndexError):
                pass

    logger.info("Chroma query returned %d id(s)", len(ids))
    return ids, id_to_distance


# ---------------------------------------------------------------------------
# Vertex AI Vector Search (legacy / cloud path)
# ---------------------------------------------------------------------------

def _search_vertex(
    query_embedding: List[float],
    k: int,
    cfg,
    source_type_allow: List[str] | None = None,
) -> tuple[List[str], dict[str, float]]:
    """Query Vertex AI Vector Search, return (ids, id_to_distance)."""
    rag = cfg.rag

    # Build Vertex filter from config
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
        response = endpoint.find_neighbors(
            deployed_index_id=rag.vertex_deployed_index_id,
            queries=[query_embedding],
            num_neighbors=k,
            filter=filters if filters else None,
        )
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
        return ids, id_to_distance
    except Exception as e:
        logger.exception("Vertex find_neighbors failed: %s", e)
        return [], {}


# ---------------------------------------------------------------------------
# Main search entry point
# ---------------------------------------------------------------------------

def search_published_rag(
    question: str,
    k: int = 10,
    confidence_min: float | None = None,
    source_type_allow: List[str] | None = None,
    emitter: Callable[[str], None] | None = None,
) -> List[dict[str, Any]]:
    """Search published RAG: embed question (1536), query vector store
    (Chroma or Vertex) with filters, fetch metadata from Postgres by id.

    If confidence_min is set, only return chunks with confidence >=
    confidence_min (after fetching k). If source_type_allow is set,
    restrict results to those source_type values. Returns list of dicts
    with keys: text, document_id, document_name, page_number,
    source_type, and — when a vector distance was returned —
    match_score / confidence.

    skills-core refactor (Day 4, 2026-04-20)
    ----------------------------------------
    The vector-search + Postgres metadata hydration moved to
    ``mobius_skills_core.skills.corpus_search.run_corpus_search``.
    This function is now a thin chat-specific adapter that:

      * reads chat's config + embedding provider (chat-specific)
      * constructs the shared skill's config dataclasses
      * calls the shared skill
      * applies chat's confidence_min filter
      * returns the dict shape downstream chat code expects
        (answer_non_patient, doc_assembly, etc.)

    Keeps the existing return shape so all chat callers work unchanged.
    Over time the confidence filter + doc_assembly can also migrate to
    shared core; today's scope is the pure retrieval layer.
    """
    from app.chat_config import get_chat_config
    from app.db_client import db_query as db_query_fn
    from app.services.embedding_provider import get_query_embedding
    from mobius_skills_core.skills.corpus_search import (
        ChromaConfig,
        CorpusFilters,
        VertexConfig,
        run_corpus_search,
    )

    cfg = get_chat_config()
    rag = cfg.rag

    # Determine backend + build the config dataclass the shared skill
    # understands. Config lives in chat's chat_config; skills-core stays
    # env-neutral.
    use_chroma = rag.vector_store == "chroma"
    chroma_cfg: ChromaConfig | None = None
    vertex_cfg: VertexConfig | None = None
    if use_chroma:
        if not rag.chroma_persist_dir or not rag.database_url:
            logger.warning("Published RAG: chroma_persist_dir or database_url not set")
            return []
        chroma_cfg = ChromaConfig(
            persist_dir=rag.chroma_persist_dir,
            collection=rag.chroma_collection or "published_rag",
        )
    else:
        if not rag.vertex_index_endpoint_id or not rag.vertex_deployed_index_id or not rag.database_url:
            logger.warning(
                "Published RAG: vertex_index_endpoint_id, vertex_deployed_index_id, or database_url not set"
            )
            return []
        vertex_cfg = VertexConfig(
            project_id=cfg.llm.vertex_project_id,
            location=(cfg.llm.vertex_location or "us-central1"),
            index_endpoint_id=rag.vertex_index_endpoint_id,
            deployed_index_id=rag.vertex_deployed_index_id,
        )

    # Legacy emit for UI parity — the shared skill also emits structured
    # SkillEvents, but the chat's existing emit channel expects short
    # strings. Kept for pre-Day-5 callers that haven't migrated to the
    # envelope translator yet.
    _emit(emitter, "Getting your question ready to search...")

    filters = CorpusFilters(
        payer=rag.filter_payer or "",
        state=rag.filter_state or "",
        program=rag.filter_program or "",
        authority_level=rag.filter_authority_level or "",
        source_type_allow=source_type_allow,
    )

    result = run_corpus_search(
        query=question,
        embed_query=get_query_embedding,
        k=k,
        filters=filters,
        chroma=chroma_cfg,
        vertex=vertex_cfg,
        database="chat",
        db_query_fn=db_query_fn,
        # emitter deliberately NOT passed — chat's string emitter can't
        # accept SkillEvents. Day 5+ will plug the SkillEvent→EmitEnvelope
        # translator here so structured retrieval emits land in the
        # thinking log with correlation_id / task-manager hints.
    )

    if result.signal == "tool_error":
        logger.warning("Published RAG shared skill returned tool_error: %s", result.text)
        return []

    # Convert SkillResult.chunks back to the dict shape chat callers
    # expect. Preserves every field the legacy function returned:
    # id, text, document_id, document_name, page_number,
    # paragraph_index, source_type, distance, match_score, confidence.
    ordered: list[dict[str, Any]] = []
    for chunk in result.chunks:
        md = chunk.metadata or {}
        distance = md.get("distance")
        match_score = chunk.score if distance is not None else None
        confidence = match_score
        ordered.append({
            "id": chunk.chunk_id or None,
            "text": chunk.text,
            "document_id": chunk.document_id or None,
            "document_name": chunk.document_name,
            "page_number": chunk.page_number,
            "paragraph_index": md.get("paragraph_index"),
            "source_type": md.get("source_type") or "chunk",
            "distance": distance,
            "match_score": match_score,
            "confidence": confidence,
        })

    # Confidence filter stays in chat — still chat-specific taxonomy.
    # Will move to shared core in a Day 5+ follow-up once we pull in
    # the full doc_assembly pipeline.
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
    """Hierarchical retrieval: ask vector store for neighbors with source_type in [policy, section, chunk, hierarchical].
    Mart may use "hierarchical" vs "fact"; we request all non-fact types. If the index exposes source_type,
    we get k results from the vector store. If 0 results, fall back to fetch more then sort in code.
    """
    # Exclude fact so vector store returns only hierarchical types (policy, section, chunk, or hierarchical)
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
        "Falling back to fetch-then-sort. Populate source_type in vector store for true hierarchical retrieval.",
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
    combined: List[dict[str, Any]] = []
    if n_hierarchical > 0:
        H = search_hierarchical(question, k=n_hierarchical, emitter=emitter)
        combined.extend(H)
    if n_factual > 0:
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
