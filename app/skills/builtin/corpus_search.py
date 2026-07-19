"""Builtin skill: search_corpus.

Per spec from the rag agent (2026-04-28). Replaces today's chat-side
hybrid (BM25 ⊕ vector) which never had a real BM25 arm in production
post-Chroma — see docs/CORPUS_RETRIEVAL_SKILL_EXTRACTION_PLAN.md and
the 22% completion-rate baseline that drove this extraction.

What this file does
-------------------

* HTTP client for ``POST {RAG_API_URL}/api/skills/v1/corpus_search_agent``.
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


_VALID_MODES = ("corpus", "precision", "recall", "auto", "d")
_VALID_ASSEMBLY = ("score", "canonical_first", "balanced")
_DEFAULT_K = 10
_HTTP_TIMEOUT_S = 120.0
# Endpoint shape per the rag-agent spec (2026-04-28, refined):
#
#   POST {RAG_API_URL}/api/skills/v1/corpus_search
#   Content-Type: application/json
#   {"query": ..., "caller": "mobius_chat", ...}
#
# Direct call to rag — no gateway in dev. The earlier intent was to
# proxy through mobius-os for caller attribution, but rag now writes
# search_events.caller from the body field, so the indirection adds
# no value. If/when prod adds X-Skill-Token auth, only this file
# changes.
_SKILL_PATH = "/api/skills/v1/corpus_search_agent"
_CALLER = "mobius_chat"


def _resolve_base_url() -> str | None:
    """Resolve the rag service base URL.

    ``RAG_API_URL`` is what every other chat caller already uses
    (curator tools, retriever_backend's legacy path, etc.). Same
    knob, no new env to manage.
    """
    url = (os.environ.get("RAG_API_URL") or "").strip()
    return url or None


def _post_skill(
    *,
    base_url: str,
    query: str,
    k: int,
    mode: str,
    filters: dict[str, Any],
    include_document_ids: list[str] | None,
    assembly_strategy: str | None,
    canonical_floor: float | None,
    caller_id: str | None,
) -> dict[str, Any]:
    """POST to rag's v1 corpus_search_agent skill.

    Returns the full CorpusSearchAgentResponse JSON which includes
    ``chunks``, ``telemetry``, and the rich pipeline trace fields:
    ``query_profile``, ``routing``, ``themes``, ``candidate_pool``.
    Raises on HTTP / network / JSON errors; caller maps to a
    ``no_sources`` envelope.

    Caller attribution is sent THREE ways for cross-rev compatibility:
      * Body field   ``caller``       — original spec
      * Header       ``X-Caller``     — newer spec, used by
                                        search_events writer
      * Header       ``X-Caller-Id``  — per-request unique id, lets
                                        rag correlate a search row
                                        with the chat turn that
                                        triggered it (chat passes
                                        the turn correlation_id)
    """
    url = base_url.rstrip("/") + _SKILL_PATH
    body: dict[str, Any] = {
        "query": query,
        "k": int(k),
        "filters": filters or {},
        # Chat has its own LLM — skip the agent's internal synthesis to
        # avoid paying for two LLM calls per search round (~20-30s saved).
        "skip_synthesis": True,
    }
    # Pass mode as a strategy override when explicitly set (precision/recall).
    # The agent picks its own strategy when mode is "corpus" / unset —
    # that gives the router more signal. Explicit arms still win.
    if mode and mode != "corpus":
        body["mode"] = mode
    if include_document_ids:
        body["include_document_ids"] = list(include_document_ids)
    # assembly_strategy / canonical_floor are not in CorpusSearchAgentRequest;
    # the agent owns assembly internally. Pass them as caller_mode hints
    # instead so the router can factor them into its preference resolution.
    if assembly_strategy:
        body["caller_mode"] = assembly_strategy
    if canonical_floor is not None:
        body["accuracy_need"] = float(canonical_floor)
    payload = json.dumps(body).encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "X-Caller": _CALLER,
    }
    if caller_id:
        headers["X-Caller-Id"] = str(caller_id)
    req = urllib.request.Request(
        url,
        data=payload,
        headers=headers,
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


_US_STATES_TO_CODE: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}


def _normalize_state(s: str | None) -> str | None:
    """Canonicalize a state value to its 2-letter USPS code.

    The corpus tags documents with 2-letter state codes ("FL"). Chat's
    active_context typically stores the full name ("Florida") because
    that's what the user / planner emits. Without normalization, every
    chat search with a state filter returns 0 chunks (observed
    2026-04-28: cid=28b2ae20, "timely filing window Florida Medicaid"
    → bm25_hits=0 vector_hits=0 because filters={'state':'Florida'}).

    Behavior:
      "Florida"  → "FL"
      "florida"  → "FL"
      "FL"       → "FL"
      "fl"       → "FL"
      "Floor"    → "Floor"   (unknown — pass through, server may match)
      None / "" → None
    """
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    if len(s) == 2:
        return s.upper()
    code = _US_STATES_TO_CODE.get(s.lower())
    return code or s


def _filters_from_active(active: dict[str, Any] | None) -> dict[str, Any]:
    """Extract the four jurisdiction filters from the chat thread's
    active context. State is normalized to the 2-letter USPS code so
    the rag-side tag filter matches (corpus uses "FL" not "Florida").
    Empty / None entries are dropped before sending."""
    a = active or {}
    raw_state = a.get("state") or a.get("jurisdiction") or ""
    out = {
        "payer": (a.get("payer") or "").strip() or None,
        "state": _normalize_state(str(raw_state)),
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
    # Read both the refined (2026-04-28) and earlier-draft shapes so
    # the adapter works regardless of which rag rev is live:
    #   refined:  telemetry.arm_hits.{bm25,vector} + telemetry.total_ms
    #   draft:    telemetry.arms.{bm25_hits,vec_hits} + telemetry.timing.*
    t = telemetry or {}
    arm_hits = t.get("arm_hits") or {}
    arms_legacy = t.get("arms") or {}
    timing_legacy = t.get("timing") or {}
    bm25_n = int(arm_hits.get("bm25") or arms_legacy.get("bm25_hits") or 0)
    vec_n = int(arm_hits.get("vector") or arms_legacy.get("vec_hits") or 0)
    returned = int(arms_legacy.get("returned") or len(chunks) or 0)
    bm25_ms = float(timing_legacy.get("bm25_ms") or 0.0)
    vec_ms = float(timing_legacy.get("vec_ms") or 0.0)
    rerank_ms = float(timing_legacy.get("rerank_ms") or 0.0)
    total_ms = float(t.get("total_ms") or timing_legacy.get("total_ms") or 0.0)
    legacy_trace: dict[str, Any] = {
        "extract": {
            "bm25_raw_n": bm25_n,
            "vector_raw_n": vec_n,
            "merged_n": returned,
            "extract_ms": int(bm25_ms + vec_ms + 0.5),
            "bm25_normalized_query": t.get("bm25_normalized_query"),
        },
        "merge": {
            "n_added_bm25": bm25_n,
            "n_added_vector": vec_n,
        },
        "rerank": {
            "rerank_ms": int(rerank_ms + 0.5),
            "n_chunks_input": bm25_n + vec_n,
            "n_chunks_after_decay": returned,
        },
        "blend_selection": {
            "chunks_input_n": bm25_n + vec_n,
            "n_output": returned,
        },
        "n_assembled": returned,
        "n_corpus": returned,
        "n_google": 0,
        "assemble_ms": int(total_ms + 0.5),
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
        if assembly_strategy not in (None,) + _VALID_ASSEMBLY:
            logger.warning(
                "corpus_search: unknown assembly_strategy=%r; passing through",
                assembly_strategy,
            )

    canonical_floor = inputs.get("canonical_floor")
    if canonical_floor is not None:
        try:
            canonical_floor = float(canonical_floor)
        except (TypeError, ValueError):
            canonical_floor = None

    include_document_ids = inputs.get("include_document_ids")
    if include_document_ids and not isinstance(include_document_ids, list):
        include_document_ids = None

    base_url = _resolve_base_url()
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
        payer = filters.get("payer") or ""
        state = filters.get("state") or ""
        ctx_label = " · ".join(x for x in [payer, state] if x)
        qshort = (query[:55] + "…") if len(query) > 55 else query
        if ctx_label:
            call.emitter(f"◌ Searching {ctx_label} materials for \"{qshort}\"…")
        else:
            call.emitter(f"◌ Searching policy docs for \"{qshort}\"…")

    # X-Caller-Id = the chat turn correlation_id when present, else
    # a fresh uuid. Lets rag correlate a search_events row with the
    # chat turn that triggered it (the lexicon-coaching feed reads
    # this for "which turn surfaced the unmatched phrase").
    caller_id = getattr(call.pipeline_ctx, "correlation_id", None) or str(uuid.uuid4())

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
            canonical_floor=canonical_floor,
            caller_id=caller_id,
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

    # Build full pipeline_trace matching the canonical schematic:
    # gate → parser → pool → bandit → retrieval → assembler.
    # Merged into telemetry so make_retrieval_trace and the chat UI
    # render the identical trace the RAG test tab shows.
    strategies_tried = resp.get("strategies_tried") or []
    _primary = strategies_tried[0] if strategies_tried else {}
    _arms = _primary.get("arms") or {}
    _timing = _arms.get("timing_ms") or {}

    _pipeline_trace: dict[str, Any] = {
        # [1] Gate
        "gate": resp.get("gate") or {
            "passed": resp.get("strategy_used") != "e",
            "fail_fast_reason": (resp.get("fail_fast") or {}).get("reason"),
        },
        # [2] Parser
        "parser": {
            **(resp.get("query_profile") or {}),
        },
        # [3] Pool
        "pool": resp.get("candidate_pool") or {},
        # [4] Bandit / router decision
        "bandit": {
            "strategy_picked": resp.get("strategy_used"),
            "forced": bool(mode),
            "routing": resp.get("routing") or {},
        },
        # [5] Retrieval — per-arm breakdown from primary strategy
        "retrieval": {
            "strategies_tried": strategies_tried,
            "arm_hits": {
                "bm25": sum((s.get("arms") or {}).get("bm25_pool_hits", 0) or 0 for s in strategies_tried),
                "vector": sum((s.get("arms") or {}).get("vector_pool_hits", 0) or 0 for s in strategies_tried),
            },
            "top_rerank": _primary.get("top_rerank"),
            "timing_ms": _timing,
        },
        # [6] Assembler
        "assembler": {
            "n_chunks": len(chunks),
            "confidence": resp.get("confidence"),
            "strategy_used": resp.get("strategy_used"),
            "total_ms": (resp.get("telemetry") or {}).get("total_ms"),
        },
        # Pass-through fields the UI already uses
        "strategy_used": resp.get("strategy_used"),
        "themes": resp.get("themes"),
        "theme_diagnostic": resp.get("theme_diagnostic"),
        "queries_per_strategy": resp.get("queries_per_strategy"),
        # Raw RAG response fields, FLAT — the chat retrieval-trace UI
        # (frontend/src/app.ts renderRetrievalTrace) reads these by the
        # SAME names the rag agent's own frontend uses. Without the flat
        # pass-through the Parser / Router / Cascade / Strategy sections
        # read ``undefined`` and silently render nothing — which is why
        # only themes + rewrite showed and the panel looked "simpler".
        "query_profile": resp.get("query_profile"),
        "routing": resp.get("routing"),
        "candidate_pool": resp.get("candidate_pool"),
        "term_partition": resp.get("term_partition"),
        # RAG-provided reframe hint (populated when fast_exit=true or all
        # strategies missed). Chat surfaces this to the ReAct LLM so it
        # can reframe from learned signal rather than paraphrasing blindly.
        "improvement_hint": resp.get("improvement_hint"),
        "fast_exit": resp.get("fast_exit", False),
        "strategies_tried": strategies_tried,
        "confidence": resp.get("confidence"),
        # Slim per-chunk projection for the UI "Reranking" table (§9) —
        # metadata only (no text; the chunk bodies already render as
        # citation sources). Mirrors the rag agent UI's rerank table.
        "reranked_chunks": [
            {
                "rank": _i,
                "document_name": _c.get("document_name"),
                "page_number": _c.get("page_number"),
                "retrieval_arms": _c.get("retrieval_arms") or [],
                "rerank_score": _c.get("rerank_score"),
                "similarity": _c.get("similarity"),
                "authority_level": _c.get("authority_level"),
                # For the Assembly (§10) context-adequacy stats: length,
                # confidence tier, and whether a section path was resolved.
                "text_len": len(str(_c.get("text") or "")),
                "confidence_label": _c.get("confidence_label"),
                "section_path": _c.get("section_path"),
            }
            for _i, _c in enumerate(chunks, 1)
        ],
        # arm_hits = FINAL returned arm split (from each chunk's
        # ``retrieval_arms``), NOT pool-stage hits — so the badge shows
        # e.g. "pgvector 10" to match the rag UI's "arm split: vector=10"
        # instead of the misleading "0".
        "arm_hits": {
            "bm25": sum(1 for c in chunks if "bm25" in (c.get("retrieval_arms") or [])),
            "vector": sum(1 for c in chunks if "vector" in (c.get("retrieval_arms") or [])),
        },
        "arms": {"returned": len(chunks)},
    }
    telemetry = {**telemetry, **_pipeline_trace}

    # Always emit the retrieval_trace envelope, even on zero hits — the
    # technical UI panel needs to show "BM25 0 vec 0" for failure
    # modes, not silently elide the diagnostic.
    _emit_retrieval_trace_envelope(
        call=call,
        search_id=search_id,
        query=query,
        mode=resp.get("strategy_used") or mode,
        k=k,
        telemetry=telemetry,
    )

    # ── Fact-store fast-exit (strategy "s") ─────────────────────────────
    # RAG's payor fact store serves pre-certified deterministic facts with
    # n_chunks=0 (no retrieval needed — the fact IS the answer). If we fall
    # through to the chunk-based path the LLM will re-synthesise from empty
    # evidence and produce "Not found." instead of the certified answer.
    # Fast-exit: return llm_answer directly as a corpus_only hit, score 1.0.
    _routing = resp.get("routing") or {}
    _strategy_s = (
        resp.get("strategy_used") == "s"
        or _routing.get("method") == "fact_store"
    )
    if _strategy_s:
        _fact_answer = str(resp.get("llm_answer") or "").strip()
        if _fact_answer:
            if call.emitter:
                call.emitter(f"📋 Certified fact: {_fact_answer}")
            return SkillEnvelope(
                text=_fact_answer,
                sources=[],
                signal="corpus_only",
                extra={
                    "pipeline_trace": telemetry,
                    "skill_call_ms": elapsed_ms,
                    "search_id": search_id,
                    "mode": "s",
                    "fact_score": resp.get("fact_score") or 1.0,
                    "fact_predicate": resp.get("fact_predicate"),
                    "fact_cert_grades": _routing.get("fact_cert_grades"),
                    "confidence": resp.get("confidence"),
                },
            )

    if not chunks:
        if call.emitter:
            strategy_used = resp.get("strategy_used") or ""
            gate = resp.get("gate") or {}
            if strategy_used == "e" or gate.get("fail_fast_reason"):
                fail_reason = gate.get("fail_fast_reason") or "no_domain_match"
                call.emitter(f"⚡ Query outside current coverage ({fail_reason}) — no corpus search run.")
            else:
                strats = resp.get("strategies_tried") or []
                n_tried = len(strats)
                if n_tried > 1:
                    call.emitter(f"↓ Tried {n_tried} search strategies — nothing matched in our materials.")
                else:
                    call.emitter("↓ Nothing matched in our materials.")
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
    # Map every field rag returns into SourceRef.extra so downstream
    # consumers (integrator citation rendering, retrieval_runs writer,
    # technical-mode UI panel) all see the full chunk shape without
    # reaching back into pipeline_trace.
    sources: list[SourceRef] = []
    for i, c in enumerate(chunks, 1):
        # ``source_type`` from rag is "hierarchical" / "fact" — that's
        # the chunk's structural grain. Map it onto SourceRef's
        # ``source_type`` field where the chat shape uses
        # "document" / "web" / etc. We pin to "document" here and
        # stash rag's grain under extra.chunk_grain so neither
        # consumer is confused.
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
                    "similarity": c.get("similarity"),
                    "confidence_label": c.get("confidence_label"),
                    "retrieval_arms": c.get("retrieval_arms") or [],
                    "jpd_tags": c.get("jpd_tags") or [],
                    "paragraph_index": c.get("paragraph_index"),
                    "chunk_grain": c.get("source_type"),
                    "payer": c.get("payer"),
                    "state": c.get("state"),
                },
            )
        )

    # ── Register post-synthesis grading callback ─────────────────────
    # _observe_async on the RAG side is called before chat synthesis
    # (skip_synthesis=True), so synthesis_grade is NULL on prod rows.
    # After the chat LLM generates the final answer, _publish_completed
    # fires these callbacks to PATCH the row with grounding grades.
    _rag_agent_id = telemetry.get("agent_id") or ""
    if _rag_agent_id and base_url and chunks:
        _pending = getattr(call.pipeline_ctx, "pending_rag_grade_calls", None)
        if _pending is not None:
            _pending.append({
                "base_url": base_url,
                "rag_agent_id": _rag_agent_id,
                "query": query,
                "chunks": chunks,
            })

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
    # Rag's response shape (refined 2026-04-28) puts arm counts under
    # ``telemetry.arm_hits.bm25 / .vector``. The earlier draft used
    # ``arms.bm25_hits / .vec_hits`` — we read both keys so the emit
    # works against whichever rag rev is live without coordination.
    if call.emitter:
        ret_n = len(chunks)
        # Unique source documents for "from N docs" label
        unique_docs = len({c.get("document_name") or "" for c in chunks if c.get("document_name")})
        doc_label = f" across {unique_docs} doc{'s' if unique_docs != 1 else ''}" if unique_docs > 1 else ""
        # Top relevance score
        top_score = max((c.get("rerank_score") or 0.0) for c in chunks) if chunks else 0.0
        score_label = f" · top match {top_score:.0%}" if top_score > 0 else ""
        call.emitter(
            f"✓ Found {ret_n} relevant passage{'s' if ret_n != 1 else ''}{doc_label}{score_label}"
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
                "enum": list(_VALID_ASSEMBLY),
            },
            "canonical_floor": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
        },
    },
    requires_jurisdiction=True,
    follow_up_capable=True,
    source="builtin",
    visible_to_planner=True,
    category="corpus",
    display_name="Corpus Search",
)


register(SPEC)
