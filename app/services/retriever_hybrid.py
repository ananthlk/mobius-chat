"""Hybrid retrieval — BM25 ⊕ vector via Reciprocal Rank Fusion (RRF).

2026-04-24 (Sprint 2 #0.2). Until today the chat's primary
``search_corpus`` tool ran BM25 only (via
``retriever_backend.retrieve_for_chat``). The vector path
(``published_rag_search.search_published_rag`` — Chroma) existed as
dead code, never reached on a turn. We had a 1168-vector index sitting
idle while pure keyword search was the only signal. Paraphrased
questions ("challenge a non-payment decision" instead of "appeal
denied claim") missed the corpus entirely.

This module fuses both signals into one ranked list and surfaces the
result through three call modes, mapped to chat's tool taxonomy:

  * ``corpus``    — hybrid BM25 ⊕ vector (default; see ``retrieve_corpus_hybrid``).
                    Best for most turns: keeps BM25 exact-phrase wins
                    AND vector semantic recall on paraphrases.
  * ``recall``    — vector-only, no confidence floor, higher k. Used by
                    ``recall_search`` (was ``lazy_corpus_search``).
                    Best for "what do we know about X" exploratory passes.
  * ``precision`` — BM25-only, exact-phrase boost. Used by
                    ``precision_search`` (new). Best for code/ID lookups
                    (HCPCS, FL.UM.87, exact policy numbers).

Fusion algorithm
----------------
We use Reciprocal Rank Fusion (Cormack et al., 2009) with k=60:

    rrf_score(d) = Σ_arms  1 / (k + rank_arm(d))

RRF was picked over score-normalized fusion because:
  - It's parameter-free at the chunk level (no per-arm score
    normalization, which we'd otherwise have to recalibrate every
    time a reranker config changes).
  - It tolerates very different score distributions across arms — BM25
    sigmoid scores and vector cosine similarities aren't comparable
    on raw values, but their ranks are.
  - It's robust to one arm returning fewer results than the other:
    the missing arm just contributes 0, the present arm dominates.

Canonical vs factual blend
--------------------------
After RRF we apply ``mobius_retriever.assemble._apply_blend_selection``
exactly as the BM25-only path does today. The blend selects:

    n_hierarchical paragraph slots (canonical / policy) first, then
    n_factual sentence slots (specific facts).

The fusion just gives the blend a richer source pool (vector hits join
BM25 hits in the candidate set); the slot-allocation logic is unchanged.

Source attribution
------------------
Every fused chunk carries ``retrieval_arms`` in its metadata:
``["bm25"]``, ``["vector"]``, or ``["bm25", "vector"]`` when both
arms surfaced it. This lets downstream telemetry track what each arm
contributed without losing per-arm scores.
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

# RRF constant — Cormack et al. 2009 recommend 60. Higher k = flatter
# rank decay (less aggressive top-rank reward). 60 has held up across
# many retrieval benchmarks; we match it.
_RRF_K = 60

# Stable id → chunk mapping during fusion. Chunks may be returned by
# both arms with the same id; we merge by id and combine arm metadata.
_RetrievalArm = str  # "bm25" | "vector"


# ── RRF fusion ────────────────────────────────────────────────────────


def _rrf_merge(
    arms: dict[_RetrievalArm, list[dict[str, Any]]],
    *,
    k: int = _RRF_K,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion. Returns chunks ordered by RRF score (desc).

    ``arms`` maps arm name → ranked chunk list (rank 1 = top). Chunks
    must carry an ``id`` field (uuid string). Arms can be different
    lengths; missing arms contribute 0 for that chunk.

    Each output chunk gets:
      * ``rrf_score``         — fused score, monotonic with quality
      * ``retrieval_arms``    — list of arms that surfaced it
      * ``arm_ranks``         — {arm_name: rank} for diagnostics
      * ``arm_scores``        — {arm_name: original_match_score}
    Other fields are taken from the first arm that surfaced the chunk
    (later arms only fill missing fields, never overwrite text/metadata).
    """
    fused: dict[str, dict[str, Any]] = {}
    for arm_name, ranked in arms.items():
        for rank0, chunk in enumerate(ranked):
            cid = str(chunk.get("id") or "")
            if not cid:
                continue
            rank1 = rank0 + 1   # 1-indexed for RRF formula
            contribution = 1.0 / (k + rank1)

            if cid not in fused:
                # Seed with a copy so subsequent arms don't mutate the source list
                fused[cid] = dict(chunk)
                fused[cid].setdefault("retrieval_arms", [])
                fused[cid].setdefault("arm_ranks", {})
                fused[cid].setdefault("arm_scores", {})
                fused[cid]["rrf_score"] = 0.0
            else:
                # Fill missing fields from the new arm (don't overwrite)
                for key, val in chunk.items():
                    if key in ("retrieval_arms", "arm_ranks", "arm_scores"):
                        continue
                    if fused[cid].get(key) in (None, "", []) and val not in (None, "", []):
                        fused[cid][key] = val

            arms_list = fused[cid]["retrieval_arms"]
            if arm_name not in arms_list:
                arms_list.append(arm_name)
            fused[cid]["arm_ranks"][arm_name] = rank1
            score = chunk.get("match_score")
            if isinstance(score, (int, float)):
                fused[cid]["arm_scores"][arm_name] = float(score)
            fused[cid]["rrf_score"] += contribution

    # Sort by RRF score desc; ties broken by best (lowest) rank across arms
    def _sort_key(c: dict[str, Any]) -> tuple[float, int]:
        ranks = c.get("arm_ranks") or {}
        best_rank = min(ranks.values()) if ranks else 999
        return (-float(c.get("rrf_score") or 0.0), best_rank)

    out = sorted(fused.values(), key=_sort_key)

    # Promote the fused score to ``match_score`` so downstream
    # confidence-filter callers (which read match_score / confidence)
    # see a comparable [0, ~0.033] scalar. We DON'T touch ``confidence``
    # — that field is the per-chunk confidence label flowing through
    # doc_assembly. Keeping rrf_score separate preserves observability.
    for c in out:
        c["match_score_rrf"] = c["rrf_score"]
        # Keep original arm match_scores too — downstream confidence
        # filter can still inspect arm_scores when match_score is RRF.
    return out


# ── Arm runners ──────────────────────────────────────────────────────


def _run_bm25_arm(
    question: str,
    *,
    top_k: int,
    database_url: str,
    filter_payer: str,
    filter_state: str,
    filter_program: str,
    filter_authority_level: str,
    n_factual: int | None,
    n_hierarchical: int | None,
    emitter: Callable[[str], None] | None,
    include_document_ids: list[str] | None,
) -> list[dict[str, Any]]:
    """Run BM25 path via the existing retriever_backend logic.

    Reuses ``retrieve_for_chat`` with the BM25-only branch — which
    means the ``RAG_API_URL`` and inline-BM25 flow stays untouched.
    Returns chunks in normalized chat shape with ``provision_type``
    and ``match_score`` populated, ready for fusion.
    """
    # Local import: retriever_backend imports us indirectly (when the
    # hybrid is wired in). Late binding sidesteps the cycle.
    from app.services.retriever_backend import retrieve_for_chat

    chunks, _trace = retrieve_for_chat(
        question=question,
        top_k=top_k,
        database_url=database_url,
        filter_payer=filter_payer,
        filter_state=filter_state,
        filter_program=filter_program,
        filter_authority_level=filter_authority_level,
        n_factual=n_factual,
        n_hierarchical=n_hierarchical,
        emitter=emitter,
        include_trace=False,
        include_document_ids=include_document_ids,
        _hybrid_internal=True,   # signal: don't recurse into hybrid
    )
    for c in chunks or []:
        c["_arm_origin"] = "bm25"
    return chunks or []


def _run_vector_arm(
    question: str,
    *,
    top_k: int,
    confidence_min: float | None,
    source_type_allow: list[str] | None,
    emitter: Callable[[str], None] | None,
) -> list[dict[str, Any]]:
    """Vector arm — RETIRED 2026-04-27 post-pgvector cutover.

    Until the cutover this called ``search_published_rag`` which talked
    directly to the chat-owned Chroma VM (collection ``published_rag``).
    Two things made that path wrong post-cutover:

    1. The Chroma VM is no longer the source of truth — pgvector is.
       Anything Chroma returned was at best stale, at worst phantoms
       (doc_ids deleted from Postgres but still indexed in Chroma).
    2. Connection-level instability on the e2-micro VM caused 2+ minute
       TCP hangs per call (observed 2026-04-27 cid=fb0726d8: 138s gap
       between BM25 arm returning and the vector arm finally erroring
       out with "Connection reset by peer"). That alone consumed half
       the 300s ReAct turn budget, leading to ``turn_deadline_exceeded``
       on every call.

    The fix is not a timeout (that hides the dead path); it's removing
    the call. The "BM25" arm in this hybrid already routes through
    mobius-rag's ``/api/query`` which now serves pgvector results, so
    vector recall is preserved via that single path. The hybrid scaffold
    is kept so we can plug a proper second arm back in later (e.g. a
    second ``/api/query`` call with different params for dual-pass
    recall, or a re-ranker arm) without re-introducing direct Chroma.
    """
    logger.info(
        "hybrid: vector arm is a no-op (chat-side Chroma path retired "
        "post-pgvector cutover; recall now flows through mobius-rag /api/query)"
    )
    return []


# ── Public entry points (one per call mode) ──────────────────────────


def retrieve_corpus_hybrid(
    question: str,
    *,
    top_k: int = 10,
    database_url: str,
    filter_payer: str = "",
    filter_state: str = "",
    filter_program: str = "",
    filter_authority_level: str = "",
    n_factual: int | None = None,
    n_hierarchical: int | None = None,
    emitter: Callable[[str], None] | None = None,
    include_document_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Hybrid corpus search — runs BM25 ⊕ vector in parallel and fuses.

    Returns ``(chunks, telemetry)``. Telemetry includes per-arm hit
    counts, fusion overlap, and timing — emit it into llm_calls or the
    thinking log so we can diagnose retrieval health per turn.

    ``n_factual`` / ``n_hierarchical`` are applied AFTER fusion via
    the same blend selection used by the BM25-only path.
    """
    import time
    t0 = time.monotonic()

    # Fan out
    arm_results: dict[str, list[dict[str, Any]]] = {"bm25": [], "vector": []}
    arm_errors: dict[str, str] = {}
    timings: dict[str, float] = {}

    def _bm25_task() -> list[dict[str, Any]]:
        ts = time.monotonic()
        try:
            r = _run_bm25_arm(
                question,
                top_k=top_k,
                database_url=database_url,
                filter_payer=filter_payer,
                filter_state=filter_state,
                filter_program=filter_program,
                filter_authority_level=filter_authority_level,
                n_factual=None,    # blend applied post-fusion
                n_hierarchical=None,
                emitter=None,      # avoid double-emit; we summarize at the end
                include_document_ids=include_document_ids,
            )
            return r
        except Exception as exc:
            arm_errors["bm25"] = f"{type(exc).__name__}: {exc}"
            logger.warning("hybrid: bm25 arm failed: %s", exc)
            return []
        finally:
            timings["bm25_ms"] = (time.monotonic() - ts) * 1000

    def _vector_task() -> list[dict[str, Any]]:
        ts = time.monotonic()
        try:
            r = _run_vector_arm(
                question,
                top_k=top_k,
                confidence_min=None,
                source_type_allow=None,
                emitter=None,
            )
            return r
        except Exception as exc:
            arm_errors["vector"] = f"{type(exc).__name__}: {exc}"
            logger.warning("hybrid: vector arm failed: %s", exc)
            return []
        finally:
            timings["vector_ms"] = (time.monotonic() - ts) * 1000

    with _cf.ThreadPoolExecutor(max_workers=2) as pool:
        f_bm25 = pool.submit(_bm25_task)
        f_vec  = pool.submit(_vector_task)
        arm_results["bm25"]   = f_bm25.result()
        arm_results["vector"] = f_vec.result()

    # RRF fuse
    fused = _rrf_merge(arm_results)

    # Blend selection: paragraph slots first, sentence slots second.
    # When neither n_* is set we just truncate to top_k.
    if (n_factual is not None or n_hierarchical is not None) and fused:
        try:
            from mobius_retriever.assemble import _apply_blend_selection
            fused = _apply_blend_selection(fused, n_factual, n_hierarchical)
        except Exception as exc:
            logger.warning("hybrid: blend selection failed (%s); using fused order", exc)
            fused = fused[: (n_factual or 0) + (n_hierarchical or 0) or top_k]
    else:
        fused = fused[:top_k]

    # Telemetry
    overlap = sum(
        1 for c in fused
        if len(c.get("retrieval_arms") or []) >= 2
    )
    telemetry: dict[str, Any] = {
        "mode": "corpus_hybrid",
        "k": top_k,
        "arm_bm25_hits": len(arm_results["bm25"]),
        "arm_vector_hits": len(arm_results["vector"]),
        "fused_count": len(fused),
        "fusion_overlap": overlap,
        "total_ms": (time.monotonic() - t0) * 1000,
        **{k: round(v, 1) for k, v in timings.items()},
    }
    if arm_errors:
        telemetry["arm_errors"] = arm_errors

    if emitter:
        if fused:
            emitter(f"Found {len(fused)} matches (BM25 {len(arm_results['bm25'])}, vector {len(arm_results['vector'])}, overlap {overlap}).")
        else:
            emitter("I didn't find anything specific in the corpus.")

    return fused, telemetry


def retrieve_recall(
    question: str,
    *,
    top_k: int = 16,
    emitter: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Vector-only, broad-recall search. No confidence filter.

    Backs ``recall_search`` (was ``lazy_corpus_search``). Used by
    agentic first-pass exploration when "what do we know about X"
    matters more than precision."""
    import time
    t0 = time.monotonic()
    chunks = _run_vector_arm(
        question, top_k=top_k, confidence_min=None,
        source_type_allow=None, emitter=emitter,
    )
    telemetry = {
        "mode": "corpus_recall",
        "k": top_k,
        "arm_vector_hits": len(chunks),
        "total_ms": (time.monotonic() - t0) * 1000,
    }
    return chunks, telemetry


def retrieve_precision(
    question: str,
    *,
    top_k: int = 10,
    database_url: str,
    filter_payer: str = "",
    filter_state: str = "",
    filter_program: str = "",
    filter_authority_level: str = "",
    n_factual: int | None = None,
    n_hierarchical: int | None = None,
    emitter: Callable[[str], None] | None = None,
    include_document_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """BM25-only, exact-phrase precision search.

    Backs ``precision_search``. Use when the user asks for a specific
    code, policy ID, or exact phrase (HCPCS, FL.UM.87, etc.) where
    keyword match dominates semantic similarity."""
    import time
    t0 = time.monotonic()
    chunks = _run_bm25_arm(
        question,
        top_k=top_k, database_url=database_url,
        filter_payer=filter_payer, filter_state=filter_state,
        filter_program=filter_program, filter_authority_level=filter_authority_level,
        n_factual=n_factual, n_hierarchical=n_hierarchical,
        emitter=emitter, include_document_ids=include_document_ids,
    )
    telemetry = {
        "mode": "corpus_precision",
        "k": top_k,
        "arm_bm25_hits": len(chunks),
        "total_ms": (time.monotonic() - t0) * 1000,
    }
    return chunks, telemetry


__all__ = [
    "retrieve_corpus_hybrid",
    "retrieve_recall",
    "retrieve_precision",
]
