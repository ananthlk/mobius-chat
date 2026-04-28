"""Builtin skill: search_corpus.

Per spec from the rag agent (2026-04-28). Replaces today's chat-side
hybrid (BM25 ⊕ vector) which never had a real BM25 arm in production
post-Chroma — see docs/CORPUS_RETRIEVAL_SKILL_EXTRACTION_PLAN.md and
the 22% completion-rate baseline that drove this extraction.

What this file does
-------------------

* HTTP client for ``POST {RAG_API_URL}/api/skills/v1/corpus_search``.
* Maps the rag service response (chunks + telemetry) into the
  ``SkillEnvelope`` shape chat consumers already understand:
  ``text`` (formatted ``[1]…[N]`` context block), ``sources``
  (``SourceRef`` list with ``rerank_score`` / ``confidence_label`` /
  ``jpd_tags`` / ``retrieval_arms`` extras), ``signal`` (``corpus_only``
  or ``no_sources``), ``extra["pipeline_trace"]`` (the rag-side
  telemetry payload, surfaced into the technical-mode UI panel).
* Emits a ``retrieval_trace`` envelope into ``thinking_log`` so the
  technical UI's existing per-event rendering picks it up under a new
  Retrieval panel (alongside ``llm_calls`` / ``qa_score`` / ``critic``).
* Persists into ``retrieval_runs`` via the legacy
  ``insert_retrieval_run`` adapter — zero schema migration, the
  v1 trace fields map cleanly onto the existing columns
  (``arms.bm25_hits → bm25_raw_n``, etc.).

What this file is NOT
---------------------

* It does not run BM25 or pgvector locally — those live server-side
  in mobius-rag now (the entire point of the extraction).
* It does not touch the chat-side ``retriever_hybrid`` /
  ``retriever_backend`` modules. Those remain as dead code for one
  more cleanup pass once we're confident the skill is healthy in
  production. (Keeping them around for the rollback ramp.)
* It does not implement upload fan-out (search_corpus + thread
  uploaded docs in parallel). That logic stays in
  ``react_loop.py`` because it spans two distinct retrieval
  surfaces — corpus (this skill) and uploaded docs
  (``instant_rag_search.lazy_rag_search``). The dispatcher there
  calls this skill for the corpus arm and the upload skill for
  each upload, then merges.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
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


_VALID_MODES = ("corpus", "precision", "recall")
_DEFAULT_K = 10
_HTTP_TIMEOUT_S = 60.0
_SKILL_PATH = "/api/skills/v1/corpus_search"


def _post_skill(
    *,
    base_url: str,
    query: str,
    k: int,
    mode: str,
    filters: dict[str, Any],
    include_document_ids: list[str] | None,
    assembly_strategy: str | None,
) -> dict[str, Any]:
    """POST to the rag service's v1 corpus_search endpoint.

    Returns the parsed JSON response (``{chunks, telemetry}``). Raises
    on HTTP / network / JSON failure; the caller maps that to a
    ``no_sources`` envelope.
    """
    url = base_url.rstrip("/") + _SKILL_PATH
    body: dict[str, Any] = {
        "query": query,
        "k": int(k),
        "mode": mode,
        "filters": filters or {},
    }
    if include_document_ids:
        body["include_document_ids"] = list(include_document_ids)
    if assembly_strategy:
        body["assembly_strategy"] = assembly_strategy
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def _format_context(chunks: list[dict[str, Any]]) -> str:
    """Number chunks ``[1]…[N]`` for the integrator/critic prompt.

    Mirrors the shape ``answer_non_patient`` / the legacy inline path
    produced, so downstream prompt templates don't need to change.
    """
    parts: list[str] = []
    for i, c in enumerate(chunks, 1):
        doc = (c.get("document_name") or "document").strip()
        page = c.get("page_number")
        text = (c.get("text") or "").strip()
        header = f"[{i}] {doc}"
        if isinstance(page, int):
            header += f" (p.{page})"
        parts.append(f"{header}\n{text}")
    return "\n\n".join(parts)


def _filters_from_active(active: dict[str, Any] | None) -> dict[str, Any]:
    """Extract the four jurisdiction filters from the chat thread's
    active context. Empty / None entries are dropped server-side."""
    a = active or {}
    out = {
        "payer": (a.get("payer") or "").strip() or None,
        "state": (a.get("state") or a.get("jurisdiction") or "").strip() or None,
        "program": (a.get("program") or "").strip() or None,
        "authority_level": (a.get("authority_level") or "").strip() or None,
    }
    return {k: v for k, v in out.items() if v}


def _persist_retrieval_run(
    *,
    correlation_id: str,
    subquestion_id: str,
    subquestion_text: str,
    telemetry: dict[str, Any],
    chunks: list[dict[str, Any]],
) -> None:
    """Adapt v1 RetrievalTracePayload → existing retrieval_runs schema.

    Zero schema migration — the rag agent's spec already maps cleanly
    onto the legacy columns:

      arms.bm25_hits         → bm25_raw_n
      arms.vec_hits          → vector_raw_n
      arms.returned          → n_assembled / blend_n_output / n_corpus
      timing.bm25_ms+vec_ms  → extract_ms
      timing.rerank_ms       → rerank_ms
      timing.total_ms        → assemble_ms (rough; legacy combined them)

    bm25_normalized_query is the only net-new field in the spec; it's
    stashed in extract for now and can promote to a real column later
    if anyone wants to query it. ``insert_retrieval_run`` ignores
    unknown extract keys, so this is forward-safe.
    """
    if not correlation_id:
        return
    timing = (telemetry or {}).get("timing") or {}
    arms = (telemetry or {}).get("arms") or {}
    bm25_ms = float(timing.get("bm25_ms") or 0.0)
    vec_ms = float(timing.get("vec_ms") or 0.0)
    legacy_trace: dict[str, Any] = {
        "extract": {
            "bm25_raw_n": arms.get("bm25_hits"),
            "vector_raw_n": arms.get("vec_hits"),
            "merged_n": arms.get("returned"),
            "extract_ms": int(bm25_ms + vec_ms + 0.5),
            "bm25_normalized_query": telemetry.get("bm25_normalized_query"),
        },
        "merge": {
            "n_added_bm25": arms.get("bm25_hits"),
            "n_added_vector": arms.get("vec_hits"),
        },
        "rerank": {
            "rerank_ms": int(float(timing.get("rerank_ms") or 0.0) + 0.5),
            "n_chunks_input": (arms.get("bm25_hits") or 0) + (arms.get("vec_hits") or 0),
            "n_chunks_after_decay": arms.get("returned"),
        },
        "blend_selection": {
            "chunks_input_n": (arms.get("bm25_hits") or 0) + (arms.get("vec_hits") or 0),
            "n_output": arms.get("returned"),
        },
        "n_assembled": arms.get("returned"),
        "n_corpus": arms.get("returned"),
        "n_google": 0,
        "assemble_ms": int(float(timing.get("total_ms") or 0.0) + 0.5),
        "path": "skill_v1",
    }
    try:
        from app.storage.retrieval_persistence import insert_retrieval_run

        insert_retrieval_run(
            correlation_id=correlation_id,
            subquestion_id=subquestion_id or "react_corpus",
            subquestion_text=(subquestion_text or "")[:2000],
            path="skill_v1",
            n_factual=None,
            n_hierarchical=None,
            trace=legacy_trace,
            assembled=chunks,
        )
    except Exception as e:
        # Persistence failure must never break the turn. Log loudly so
        # we notice if the adapter starts dropping every row, but
        # return the chunks regardless.
        logger.warning("corpus_search: insert_retrieval_run failed (%s)", e)


def _emit_retrieval_trace_envelope(
    *,
    call: SkillCall,
    search_id: str,
    query: str,
    mode: str,
    k: int,
    telemetry: dict[str, Any],
) -> None:
    """Emit the ``retrieval_trace`` envelope into the pipeline's
    thinking_log so the technical UI panel can render it.

    No-op if ``pipeline_ctx`` is missing (e.g. unit-test invocations
    without a full ReAct context). The chat-stream subscriber surfaces
    envelopes from ``ctx.thinking_chunks`` directly.
    """
    ctx = call.pipeline_ctx
    correlation_id = getattr(ctx, "correlation_id", "") or ""
    if not correlation_id:
        return
    try:
        from app.communication.emit_envelope import make_retrieval_trace
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("make_retrieval_trace import failed: %s", e)
        return
    env = make_retrieval_trace(
        correlation_id=correlation_id,
        search_id=search_id,
        query=query,
        mode=mode,
        k=k,
        telemetry=telemetry,
        round=getattr(ctx, "current_round", None),
        thread_id=getattr(ctx, "thread_id", None),
    )
    chunks = getattr(ctx, "thinking_chunks", None)
    if isinstance(chunks, list):
        chunks.append(env.to_dict())


def _run(call: SkillCall) -> SkillEnvelope:
    """search_corpus skill entry point.

    Behavior:

    1. Resolve query (input override > pipeline message), mode
       (default ``corpus``), k (default 10), assembly_strategy
       (passthrough), filters (from active jurisdiction).
    2. POST to ``{RAG_API_URL}/api/skills/v1/corpus_search``.
    3. Map response into ``SkillEnvelope`` + emit ``retrieval_trace``
       envelope + persist to ``retrieval_runs``.
    4. Return.

    Failure modes:
      * No RAG_API_URL → ``no_sources`` with explanatory text.
      * HTTP 4xx/5xx → ``no_sources`` with redacted error in
        ``extra["error"]``; UI shows "I couldn't reach our materials".
      * Empty chunks → ``no_sources`` with the telemetry preserved
        (so the panel can show "BM25 0 vec 0" diagnostics).
    """
    inputs = call.inputs if isinstance(call.inputs, dict) else {}
    query = (inputs.get("query") or call.question or call.user_message or "").strip()
    if not query:
        return SkillEnvelope(
            text="",
            sources=[],
            signal="no_sources",
            extra={"error": "empty_query"},
        )

    mode = (inputs.get("mode") or "corpus").strip().lower()
    if mode not in _VALID_MODES:
        logger.warning("corpus_search: unknown mode=%r; using 'corpus'", mode)
        mode = "corpus"

    try:
        k = int(inputs.get("k") or _DEFAULT_K)
    except (TypeError, ValueError):
        k = _DEFAULT_K
    k = max(1, min(50, k))

    assembly_strategy = inputs.get("assembly_strategy")
    if assembly_strategy is not None:
        assembly_strategy = str(assembly_strategy).strip().lower() or None
        if assembly_strategy not in (None, "score", "canonical_first", "balanced"):
            logger.warning(
                "corpus_search: unknown assembly_strategy=%r; passing through",
                assembly_strategy,
            )

    include_document_ids = inputs.get("include_document_ids")
    if include_document_ids and not isinstance(include_document_ids, list):
        include_document_ids = None

    base_url = (os.environ.get("RAG_API_URL") or "").strip()
    if not base_url:
        logger.warning("corpus_search: RAG_API_URL not set")
        if call.emitter:
            call.emitter("↓ Corpus skill not configured (RAG_API_URL unset).")
        return SkillEnvelope(
            text="",
            sources=[],
            signal="no_sources",
            extra={"error": "rag_api_url_unset"},
        )

    filters = _filters_from_active(call.active_context)

    if call.emitter:
        if mode == "precision":
            call.emitter("◌ Precision (BM25) search of our materials…")
        elif mode == "recall":
            call.emitter("◌ Broad (vector) recall of our materials…")
        else:
            call.emitter("◌ Searching our materials…")

    t0 = time.perf_counter()
    try:
        resp = _post_skill(
            base_url=base_url,
            query=query,
            k=k,
            mode=mode,
            filters=filters,
            include_document_ids=include_document_ids,
            assembly_strategy=assembly_strategy,
        )
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:300]
        except Exception:
            pass
        logger.warning("corpus_search HTTP %s: %s", e.code, body)
        if call.emitter:
            call.emitter(f"↓ Corpus search returned HTTP {e.code}.")
        return SkillEnvelope(
            text="",
            sources=[],
            signal="no_sources",
            extra={"error": f"http_{e.code}", "body": body},
        )
    except Exception as e:
        logger.warning("corpus_search transport failed: %s", e)
        if call.emitter:
            call.emitter("↓ Corpus search unavailable.")
        return SkillEnvelope(
            text="",
            sources=[],
            signal="no_sources",
            extra={"error": f"{type(e).__name__}: {e}"},
        )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    chunks = resp.get("chunks") or []
    telemetry = resp.get("telemetry") or {}
    search_id = str(uuid.uuid4())

    # Always emit the retrieval_trace envelope, even on zero hits — the
    # technical UI panel needs to show "BM25 0 vec 0" for failure
    # modes, not silently elide the diagnostic.
    _emit_retrieval_trace_envelope(
        call=call,
        search_id=search_id,
        query=query,
        mode=mode,
        k=k,
        telemetry=telemetry,
    )

    if not chunks:
        if call.emitter:
            call.emitter("↓ Not in our materials.")
        return SkillEnvelope(
            text="",
            sources=[],
            signal="no_sources",
            extra={
                "pipeline_trace": telemetry,
                "skill_call_ms": elapsed_ms,
                "search_id": search_id,
                "mode": mode,
            },
        )

    # ── Build SourceRef list ─────────────────────────────────────────
    sources: list[SourceRef] = []
    for i, c in enumerate(chunks, 1):
        sources.append(
            SourceRef(
                document_name=str(c.get("document_name") or "document"),
                index=i,
                text=str(c.get("text") or ""),
                source_type="document",
                document_id=(str(c.get("document_id") or "") or None),
                page_number=c.get("page_number"),
                authority=(str(c.get("authority_level") or "") or None),
                extra={
                    "rerank_score": c.get("rerank_score"),
                    "confidence_label": c.get("confidence_label"),
                    "jpd_tags": c.get("jpd_tags") or [],
                    "retrieval_arms": c.get("retrieval_arms") or [],
                },
            )
        )

    # ── Persist to retrieval_runs (best-effort) ──────────────────────
    correlation_id = getattr(call.pipeline_ctx, "correlation_id", "") or ""
    _persist_retrieval_run(
        correlation_id=correlation_id,
        subquestion_id=str(inputs.get("subquestion_id") or "react_corpus"),
        subquestion_text=query,
        telemetry=telemetry,
        chunks=chunks,
    )

    # ── User-facing emit ─────────────────────────────────────────────
    if call.emitter:
        arms = telemetry.get("arms") or {}
        bm25 = int(arms.get("bm25_hits") or 0)
        vec = int(arms.get("vec_hits") or 0)
        overlap = int(arms.get("overlap") or 0)
        ret_n = int(arms.get("returned") or len(chunks))
        call.emitter(
            f"  ✓ {ret_n} match{'es' if ret_n != 1 else ''} "
            f"(BM25 {bm25} · pgvector {vec} · overlap {overlap})"
        )

    return SkillEnvelope(
        text=_format_context(chunks),
        sources=sources,
        signal="corpus_only",
        extra={
            "pipeline_trace": telemetry,
            "skill_call_ms": elapsed_ms,
            "search_id": search_id,
            "mode": mode,
        },
    )


SPEC = SkillSpec(
    name="search_corpus",
    description=(
        "Hybrid corpus search across our curated knowledge base. Use for any "
        "question that should be answered from our authoritative materials "
        "(provider manuals, payer policies, Medicaid policies, etc.).\n"
        "\n"
        "Modes:\n"
        "  corpus    (default) BM25 + pgvector hybrid — best for most questions.\n"
        "  precision BM25-only, exact-phrase / code lookups (HCPCS, FL.UM.87).\n"
        "  recall    vector-only, broad scan — early-round 'what do we know about X'.\n"
        "\n"
        "Returns numbered context passages [1]…[N] plus per-chunk citations with "
        "rerank_score, confidence_label, retrieval_arms, and jpd_tags."
    ),
    handler=_run,
    inputs_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "mode": {"type": "string", "enum": list(_VALID_MODES)},
            "k": {"type": "integer", "minimum": 1, "maximum": 50},
            "assembly_strategy": {
                "type": "string",
                "enum": ["score", "canonical_first", "balanced"],
            },
        },
    },
    requires_jurisdiction=True,
    follow_up_capable=True,
    source="builtin",
    visible_to_planner=True,
)


register(SPEC)
