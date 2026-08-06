"""Builtin skill: search_corpus.

Originally per spec from the rag agent (2026-04-28), replacing chat-side
hybrid (BM25 ⊕ vector) — see docs/CORPUS_RETRIEVAL_SKILL_EXTRACTION_PLAN.md.

**Phase 1 endpoint cutover (2026-08-06, Chat Architecture spec, coordinated
with Retriever/RAG over several rounds — see project memory for the full
back-and-forth on the mode_override naming collision, the response-shape
inventory, and the fact-store "s"-chunk finding):** swapped off the legacy
``corpus_search_agent`` skill (its own separate router/CALLER_MODE_PRESETS
pipeline, being retired) onto RAG's production ``/api/retriever/answer``
endpoint — the Shape→Pool→Router→Fillers pipeline that Task #14's greedy-
allocator fix and ``authority_requirement`` actually live in.

What this file does
-------------------

* HTTP client for ``POST {RAG_API_URL}/api/retriever/answer``.
* Maps the response's ``contract.chunks[]`` into the ``SkillEnvelope``
  shape chat consumers already understand: ``text`` (formatted
  ``[1]…[N]`` context block), ``sources`` (``SourceRef`` list with
  ``rerank_score`` / ``confidence_label`` (now derived, see
  ``_derive_confidence_label``) / ``tags`` / ``retrieval_arms`` extras),
  ``signal`` (``corpus_only`` or ``no_sources``).
* Emits a ``retrieval_trace`` envelope into ``thinking_log`` (reduced
  shape post-cutover — most of the old pipeline_trace fields were
  confirmed telemetry-only with no react-side consumer; see the field
  inventory in project memory / the Chat Architecture thread).
* Persists into ``retrieval_runs`` via the legacy ``insert_retrieval_run``
  adapter, best-effort, degraded (arm-hit breakdown is gone — the new
  pipeline doesn't report bm25/vector split the same way; the adapter's
  own ``.get(..., 0)`` fallbacks handle this gracefully, not an error).

**Grading callback — resolved (2026-08-06).** The old code registered a
post-synthesis grading callback (``pending_rag_grade_calls`` →
``PATCH /api/observe/decisions/{rag_agent_id}/grade``) keyed on
``telemetry.get("agent_id")``, populating RAG's own OBSERVE-row
synthesis_grade/ledger. The new response has no equivalent field.
Retriever confirmed the grade endpoint actually filters
``WHERE correlation_id = :cid`` on the DB — chat's own turn correlation_id
(already sent in the request body, see ``_post_skill``) is the correct
key, and the callback is re-registered here + re-wired in
``orchestrator.py::_fire_rag_grade_callbacks`` to PATCH
``.../decisions/{correlation_id}/grade`` instead of
``.../decisions/{rag_agent_id}/grade``. Live against Retriever's
``persist_decision()`` fix (mobius-rag 6c124e6).

**Fact-store (``filler_strategy=="fact_store"``) chunk handling —
resolved after a Retriever pairing session (2026-08-06), per Chat
Architecture's instruction not to build this piece solo.** The old
golden-flag early-return pattern (``extra["golden"]=True`` + ``llm_answer``
as direct answer text) is removed entirely — the new response never has a
bare answer string to fast-exit on. What replaced it, confirmed against
real RAG code, not guessed:

1. **confidence_label**: hardcoded ``"high"`` for
   ``filler_strategy=="fact_store"`` chunks, bypassing
   ``_derive_confidence_label`` entirely — RAG's ``filler_s.py`` never
   sets ``rerank_score`` on these (always ``None``), and the threshold
   function would otherwise return ``"abstain"``, the worst label, for
   exactly the chunks that deserve the best one. Fact-store chunks are
   certified/verified facts, not a probabilistic retrieval match — the
   "how well did this match" question ``rerank_score`` answers doesn't
   apply. (An earlier revision checked ``filler_strategy in ("s",
   "fact_store")`` — Retriever had described the value verbally as "s,"
   which turned out to be the single-letter strategy identifier used in
   routing/taxonomy fields like ``executed_order``/``forced_strategy``, a
   different field entirely from per-chunk ``filler_strategy``, which is
   always a descriptive label. Narrowed to ``"fact_store"`` only once
   Retriever confirmed it's the sole correct value.)
2. **Authority/citation marking**: needs NO chat-side change.
   ``authority_level="contract_source_of_truth"`` is hardcoded server-side
   (``filler_s.py``) and RAG's own ``synthesis.py::_infer_authority`` uses
   ``authority_level`` FIRST when present — so ``chunk["authority"]`` is
   already ``"authoritative"`` on every fact-store chunk by the time it
   reaches chat, live-verified by Retriever. ``source_type=="fact_store"``
   (also hardcoded server-side) is the distinct marker already available
   in this file's per-chunk mapping under ``extra["chunk_grain"]`` — a
   citation UI can key off it directly for "certified fact" vs "general
   corpus passage" styling; no new field needed here.
3. **No chat-side short-circuit on seeing a fact-store chunk.** Whether
   to keep escalating (react's own round loop, or its separate auto→d
   web-search cascade) is the Router/Observer's decision, already baked
   into the response before it reaches chat — gate on the overall
   response's ``status``/``chosen_slot``/``score``/terminal signal
   (``routing_keys.routing_verdict.slots[slot_id].terminal``), never on
   strategy identity. Re-implementing a strategy-specific stop condition
   here would duplicate a decision the pipeline already made and risks
   disagreeing with it (a fact-store chunk that only partially answers a
   multi-fact query should NOT force a stop). This file makes no such
   decision today — intentionally left to react_loop.py's existing
   round/escalation logic, which is already keyed on its own state, not
   on chunk strategy identity.

What this file is NOT
---------------------

* It does not run BM25 or pgvector locally — retrieval lives entirely
  server-side in mobius-rag.
* It does not touch the chat-side ``retriever_hybrid`` /
  ``retriever_backend`` modules (separate legacy fallback path,
  untouched by this cutover).
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


_HTTP_TIMEOUT_S = 120.0
# Phase 1 cutover (2026-08-06): production, non-admin-gated endpoint on
# the Shape→Pool→Router→Fillers pipeline — confirmed live by Retriever
# (mobius-rag 15ef396). Replaces _SKILL_PATH's legacy
# /api/skills/v1/corpus_search_agent, which dispatched to a separate
# router/CALLER_MODE_PRESETS pipeline being retired.
#
#   POST {RAG_API_URL}/api/retriever/answer
#   Content-Type: application/json
#   {"query": ..., ...}
#
# Direct call to rag — no gateway in dev. Caller attribution stays on
# headers (X-Caller/X-Caller-Id) rather than a body field — unconfirmed
# whether the new endpoint reads them, kept for cross-rev safety since
# they're harmless if ignored.
_ANSWER_PATH = "/api/retriever/answer"
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
    caller_mode: str | None,
    token_budget_for_retrieval: int | None,
    citable_required: bool,
    caller_id: str | None,
) -> dict[str, Any]:
    """POST to rag's production /api/retriever/answer endpoint (Phase 1
    cutover, 2026-08-06 — see module docstring).

    Returns the full response JSON: ``{contract: {query, chosen_slot,
    score, chunks[], answer_text, thinking, traces, routing_keys,
    grounding_markers, latency_ms, attempt_count, status}, latency_ms,
    dispatch_path, allocator_override, authority_requirement,
    strategies_per_slot}``. ``answer_text``/``thinking`` are always null
    on this path (chunks-only, no synthesis — matches chat's own LLM
    doing synthesis, same assumption the legacy ``skip_synthesis=True``
    flag encoded, just no toggle needed since it's the only mode now).
    Raises on HTTP / network / JSON errors; caller maps to a
    ``no_sources`` envelope.

    Caller attribution stays on headers (X-Caller/X-Caller-Id), same as
    the legacy endpoint — unconfirmed whether the new endpoint reads
    them, harmless if ignored.

    Deliberately NOT sent (Chat Architecture ruling, 2026-08-05):
    ``allocator_override`` (chat doesn't set retrieval strategy) and
    ``authority_requirement`` (Router-internal calibrated gate; chat
    setting it separately would create two diverging governance layers).

    ``caller_mode`` (resolved 2026-08-06, Chat Architecture clarification
    — this was flagged as an open question in an earlier revision; NOT
    the LLMManager v2 "chat.default"/"chat.copilot"/"chat.thinking" speed
    vocabulary this docstring previously guessed at): the Router uses it
    for strategy weighting, and the natural value is chat's own
    quick/copilot/agentic/task ``chat_mode`` — see ``_run()``'s
    resolution (``call.mode``, which every dispatch site already sets to
    ``ctx.chat_mode``).
    """
    url = base_url.rstrip("/") + _ANSWER_PATH
    body: dict[str, Any] = {"query": query}
    if caller_mode:
        body["caller_mode"] = caller_mode
    if token_budget_for_retrieval is not None:
        body["token_budget_for_retrieval"] = int(token_budget_for_retrieval)
    if citable_required:
        body["citable_required"] = True
    # correlation_id (2026-08-06, spec amendment): the grading-callback gap
    # flagged earlier -- RAG's PATCH /observe/decisions/{id}/grade filters
    # WHERE correlation_id = :cid on the DB column. routing_keys.decision_id
    # (my first guess) maps to a different column and would 404 silently.
    # Same value already sent as X-Caller-Id (chat's own turn correlation_id
    # -- see caller_id above), now ALSO sent in the body under the exact
    # column name so Retriever's persist_decision() fix can pick it up with
    # no translation. Inert until that fix deploys (their ETA: this
    # session) -- ignored server-side until then, no second change needed
    # here once it lands.
    if caller_id:
        body["correlation_id"] = str(caller_id)
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


def _derive_confidence_label(rerank_score: float | None) -> str:
    """Phase 1 cutover (2026-08-06): the new /api/retriever/answer chunk
    shape has no pre-resolved ``confidence_label`` (the legacy endpoint
    computed this server-side) — derive it chat-side from ``rerank_score``.

    Thresholds copied from the legacy reranker's own scale (this file's
    prior ``corpus_search.py`` history, pre-cutover). Chat Architecture's
    explicit caveat: these were calibrated for the OLD reranker's score
    distribution — the new pipeline's ``rerank_score`` may be a different
    composite, or null (falls back to ``original_score`` at the call
    site). Labels are informational for citation display only, nothing
    branches on them react-side. Phase 2 recalibrates against real
    traffic once the new pipeline's score distribution is known."""
    if rerank_score is None:
        return "abstain"
    if rerank_score >= 0.55:
        return "high"
    if rerank_score >= 0.35:
        return "medium"
    if rerank_score >= 0.18:
        return "low"
    return "abstain"


def _format_context(chunks: list[dict[str, Any]]) -> str:
    """Number chunks ``[1]…[N]`` for the integrator/critic prompt.

    Mirrors the shape ``answer_non_patient`` / the legacy inline path
    produced, so downstream prompt templates don't need to change.

    Fact-store chunks get a distinct ``CERTIFIED ANSWER`` header (2026-08-06
    finding, live turn: Ananth, "why does it feel off" — full history in
    project memory / the Chat Architecture thread). Root cause: fact-store
    chunk text is a bare, cryptic value (e.g. ``"68069"``, no prose) with a
    real score of 1.0 and ``chosen_slot="direct_answer"`` — but with no
    signal in the reasoning context distinguishing it from a low-quality
    snippet, the LLM has no way to recognize it's an authoritative,
    verified answer. It gets buried next to long provider-manual prose and
    the model reasonably (from its own perspective) keeps searching for
    something that reads as more substantial, burning rounds re-querying
    an answer it already had. The old code never hit this because a
    fact-store hit used to short-circuit straight to the answer, skipping
    the reasoning loop entirely (see the module docstring's "golden-flag
    early-return REMOVED" note) — nothing replaced that signal for the
    model itself when the shortcut was removed. This does NOT reinterpret
    or expand the cryptic value (no visibility into RAG's internal
    fact-store semantics from here) — just flags it as authoritative so
    the model weighs it correctly instead of discounting it.
    """
    parts: list[str] = []
    for i, c in enumerate(chunks, 1):
        doc = (c.get("document_name") or "document").strip()
        page = c.get("page_number")
        text = (c.get("text") or "").strip()
        is_fact_store = c.get("filler_strategy") == "fact_store"
        header = f"[{i}] CERTIFIED ANSWER — {doc}" if is_fact_store else f"[{i}] {doc}"
        if isinstance(page, int):
            header += f" (p.{page})"
        if is_fact_store:
            header += "\n(This is a verified, authoritative fact from the certified fact store — trust it over general search results.)"
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

    Behavior (Phase 1 cutover, 2026-08-06 — see module docstring):

    1. Resolve query (input override > pipeline message), citable_required
       (keyword-rule output from react_loop.py), caller_mode (chat's own
       ``call.mode``/``chat_mode`` — see ``_post_skill``'s docstring),
       token_budget_for_retrieval (passthrough when explicitly supplied).
    2. POST to ``{RAG_API_URL}/api/retriever/answer``.
    3. Map ``contract.chunks[]`` into ``SkillEnvelope`` + emit a reduced
       ``retrieval_trace`` envelope + persist to ``retrieval_runs``
       (degraded — arm-hit breakdown no longer available).
    4. Return.

    Failure modes:
      * No RAG_API_URL → ``no_sources`` with explanatory text.
      * HTTP 4xx/5xx → ``no_sources`` with redacted error in
        ``extra["error"]``; UI shows "I couldn't reach our materials".
      * Empty chunks / non-ok ``contract.status`` → ``no_sources``.
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

    citable_required = bool(inputs.get("citable_required"))
    # caller_mode (2026-08-06, Chat Architecture clarification — resolves
    # the open question in _post_skill's docstring): the Router uses this
    # for strategy weighting; the natural mapping is chat's own
    # quick/copilot/agentic/task chat_mode, not a separate vocabulary.
    # ``call.mode`` already carries this — every react_loop.py dispatch
    # site sets it to ``getattr(ctx, "chat_mode", "copilot") or "copilot"``
    # (see react_loop.py's SkillCall construction), so no new plumbing is
    # needed to reach it. inputs.get("caller_mode") still wins if an
    # explicit override is ever set there.
    caller_mode = inputs.get("caller_mode") or call.mode
    caller_mode = str(caller_mode).strip() if caller_mode else None
    # token_budget_for_retrieval: passthrough only, no inference —
    # react_loop.py doesn't currently supply it.
    token_budget_for_retrieval = inputs.get("token_budget_for_retrieval")
    if token_budget_for_retrieval is not None:
        try:
            token_budget_for_retrieval = int(token_budget_for_retrieval)
        except (TypeError, ValueError):
            token_budget_for_retrieval = None

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

    # Retrieval-stage emit owned by RAG's granular progress lines
    # (understanding→searching→themes→ranking via /internal/progress).
    # Removed chat-side "◌ Searching {ctx_label} materials…" to avoid
    # double-voice and mislabeling (ctx_label = active context, not query target).

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
            caller_mode=caller_mode,
            token_budget_for_retrieval=token_budget_for_retrieval,
            citable_required=citable_required,
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

    contract = resp.get("contract") or {}
    chunks = contract.get("chunks") or []
    status = contract.get("status")
    search_id = str(uuid.uuid4())

    # Reduced telemetry (Phase 1 cutover): the old ~15-field pipeline_trace
    # was confirmed to feed ONLY the diagnostics panel (react_loop.py never
    # reads it — see the field-consumption inventory in project memory /
    # the Chat Architecture thread). This is intentionally much smaller —
    # whatever the new response actually gives us, not a reconstruction of
    # the old shape. Phase 2 re-wires the diagnostics panel against
    # traces[]/routing_keys/grounding_markers properly.
    telemetry: dict[str, Any] = {
        "chosen_slot": contract.get("chosen_slot"),
        "score": contract.get("score"),
        "status": status,
        "attempt_count": contract.get("attempt_count"),
        "latency_ms": resp.get("latency_ms") or contract.get("latency_ms"),
        "dispatch_path": resp.get("dispatch_path"),
        "allocator_override": resp.get("allocator_override"),
        "authority_requirement": resp.get("authority_requirement"),
        "n_chunks": len(chunks),
    }

    # Always emit the retrieval_trace envelope, even on zero hits — the
    # technical UI panel needs to show a failure state, not silently
    # elide the diagnostic.
    _emit_retrieval_trace_envelope(
        call=call,
        search_id=search_id,
        query=query,
        mode=str(telemetry.get("dispatch_path") or "unknown"),
        k=len(chunks),
        telemetry=telemetry,
    )

    # Fact-store (filler_strategy=="fact_store") golden-flag early-return
    # REMOVED per Chat Architecture's spec (2026-08-05) — the new response
    # never has a bare answer string to fast-exit on (answer_text/thinking
    # are always null). No chat-side short-circuit replaces it — see
    # module docstring point 3 (Retriever pairing, 2026-08-06): escalation
    # decisions belong to the Router/Observer, already reflected in the
    # response's own status/chosen_slot/terminal signal, not something
    # this file re-derives from chunk strategy identity. Fact-store chunks
    # DO get special confidence_label treatment below (module docstring
    # point 1) — that's the one real per-chunk behavior change.

    # Gate on chunks alone, NOT status. Real bug found + fixed during live
    # smoke verification (2026-08-06, this deploy): status=="partial" with
    # 10 real, useful chunks (including a fact_store hit,
    # chosen_slot="direct_answer", score=1.0) is a genuine, common,
    # successful result -- "partial" means "not every slot filled," not
    # "nothing usable." The first version of this gate treated any
    # non-"ok" status as failure and silently discarded good chunks,
    # confirmed via a direct call to the live endpoint reproducing
    # exactly what a real chat turn hit. status is still surfaced in
    # telemetry/logging for diagnostics -- just not used to discard
    # otherwise-good results.
    if not chunks:
        if call.emitter:
            if status and status not in (None, "ok", "partial"):
                call.emitter(f"↓ Retrieval status={status} — nothing usable found.")
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
            },
        )

    # ── Build SourceRef list ─────────────────────────────────────────
    # Per-chunk field mapping, Chat Architecture's spec (2026-08-06):
    #   direct:  text, document_name, document_id, page_number,
    #            rerank_score, paragraph_index, source_type
    #   renamed: authority_level -> authority
    #   derived: confidence_label (from rerank_score, see
    #            _derive_confidence_label), retrieval_arms (from
    #            filler_strategy, single-strategy-per-chunk in the new
    #            architecture, wrapped in a list to preserve shape)
    #   renamed/reshaped: jpd_tags -> tags (now a raw dict, not
    #            pre-resolved — stashed as-is; compute a summary later
    #            if a downstream consumer actually needs one)
    #   ABSENT (Phase 2, Retriever to thread through Pool→Filler→
    #            Synthesis): payer, state -- both None for now
    #   original_score is functionally similar to the old `similarity`
    #            but a different formula per the spec -- not aliased to
    #            `similarity` in extra, kept under its own name so nothing
    #            downstream mistakes it for the old metric.
    sources: list[SourceRef] = []
    for i, c in enumerate(chunks, 1):
        _filler_strategy = c.get("filler_strategy")
        # Fact-store chunks: always "high", never run through the generic
        # rerank_score threshold function (2026-08-06, Retriever
        # confirmed via code read — filler_s.py never sets rerank_score,
        # it's always None on these chunks; _derive_confidence_label(None)
        # would return "abstain", the worst label, for exactly the chunks
        # that deserve the best one. These are certified/verified facts,
        # not a probabilistic retrieval match — rerank_score's "how well
        # did this match" question doesn't apply here at all).
        #
        # "fact_store" is the ONLY correct value (Retriever confirmed,
        # 2026-08-06, resolving the dual-match this code briefly carried).
        # "s" is the single-letter strategy identifier used in routing/
        # taxonomy fields (executed_order, forced_strategy, etc.) — a
        # different field entirely from per-chunk filler_strategy, which
        # is always a descriptive label ("vector_rerank", "web_search",
        # "fact_store"), never a bare letter. The original `== "s"` check
        # was checking the wrong field's vocabulary against this one.
        _confidence_label = (
            "high" if _filler_strategy == "fact_store"
            else _derive_confidence_label(c.get("rerank_score"))
        )
        sources.append(
            SourceRef(
                document_name=str(c.get("document_name") or "document"),
                index=i,
                text=str(c.get("text") or ""),
                source_type="document",
                document_id=(str(c.get("document_id") or "") or None),
                page_number=c.get("page_number"),
                authority=(str(c.get("authority") or "") or None),
                extra={
                    "rerank_score": c.get("rerank_score"),
                    "original_score": c.get("original_score"),
                    "confidence_label": _confidence_label,
                    "retrieval_arms": [_filler_strategy] if _filler_strategy else [],
                    "filler_strategy": _filler_strategy,
                    "tags": c.get("tags") or {},
                    "paragraph_index": c.get("paragraph_index"),
                    "chunk_grain": c.get("source_type"),
                    "slot_id": c.get("slot_id"),
                    "slot_semantics": c.get("slot_semantics"),
                    "verified": c.get("verified"),
                    "is_neighbor": c.get("is_neighbor"),
                    "url": c.get("url"),
                    # Phase 2 (not yet threaded through by RAG):
                    "payer": None,
                    "state": None,
                },
            )
        )

    # ── Register post-synthesis grading callback ─────────────────────
    # Re-keyed on correlation_id (2026-08-06, Phase 1 cutover -- see
    # orchestrator.py::_fire_rag_grade_callbacks for the full history).
    # Legacy keyed this on a rag_agent_id read from the response;
    # the new endpoint has no such field. RAG's grade endpoint filters
    # WHERE correlation_id = :cid on the DB, so chat's own turn
    # correlation_id -- already known before the request even went out,
    # already sent in the request body above -- is the correct key now.
    # Retriever's persist_decision() fix (mobius-rag 6c124e6) is live,
    # so this is a real, working registration again, not a stub.
    # caller_id is the exact same value already sent in the request body's
    # correlation_id field (see _post_skill call above) -- reusing it here
    # keeps "what RAG's row was created with" and "what we PATCH against"
    # guaranteed identical, including the str(uuid.uuid4()) fallback case
    # when pipeline_ctx has no correlation_id.
    if base_url and chunks and caller_id:
        _pending = getattr(call.pipeline_ctx, "pending_rag_grade_calls", None)
        if _pending is not None:
            _pending.append({
                "base_url": base_url,
                "correlation_id": caller_id,
                "query": query,
                "chunks": chunks,
            })

    # ── Persist to retrieval_runs (best-effort, degraded) ─────────────
    # _persist_retrieval_run's own arm-hit fields fall back to 0 when
    # absent from telemetry (they always did, for cross-rev safety) --
    # this is a real reduction in what retrieval_runs captures for
    # chat-originated searches post-cutover, not a crash. Not addressed
    # by Chat Architecture's Phase 1 spec; flagged separately if needed.
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
        ret_n = len(chunks)
        unique_docs = len({c.get("document_name") or "" for c in chunks if c.get("document_name")})
        doc_label = f" across {unique_docs} doc{'s' if unique_docs != 1 else ''}" if unique_docs > 1 else ""
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
        },
    )


SPEC = SkillSpec(
    name="search_corpus",
    description=(
        "Corpus search across our curated knowledge base (Phase 1 cutover, "
        "2026-08-06 — dispatches to RAG's production Shape→Pool→Router→"
        "Fillers pipeline, strategy selection is entirely Router-owned, no "
        "mode override from chat).\n"
        "\n"
        "Returns numbered context passages [1]…[N] plus per-chunk citations "
        "with rerank_score, confidence_label (chat-derived), and "
        "retrieval_arms."
    ),
    handler=_run,
    inputs_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "citable_required": {"type": "boolean"},
            "caller_mode": {"type": "string"},
            "token_budget_for_retrieval": {"type": "integer", "minimum": 1},
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
