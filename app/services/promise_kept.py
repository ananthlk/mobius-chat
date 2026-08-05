"""AC-v2-11 — graded promise-KEPT verdicts.

Spec: docs/SPEC_AC_V2_11_PROMISE_KEPT.md (Eval-Architect, 2026-08-04).
Build against that doc verbatim — this module's docstrings summarize
intent, they aren't the source of truth for thresholds/edge cases.

Presence (AC-v2-4, structural — was the promise block assembled into
the composition) is a different question from KEPT (this module — was
the promise honored in the OUTPUT). A turn can have a validated
``hipaa_context`` block and still leak PHI; this is what catches that.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# §2b threshold — v1 value per spec; tune later against the AC-v2-11
# bank. Named constant so recalibration is a config change, not a
# code change.
GROUNDEDNESS_KEPT_THRESHOLD = 0.60

# Locked ruler for both branches (§6, non-negotiable) — the "adjudicator"
# stage (Task #25) is the one that actually produces the grounding
# sub-score §2b now reads. Resolved via model_registry so this stays a
# single source of truth if the pin ever moves.
_RULER_STAGE = "adjudicator"

_PHI_CLASSIFY_TIMEOUT = float(os.environ.get("MOBIUS_PROMISE_KEPT_PHI_TIMEOUT_S", "20"))


@dataclass
class PerPromiseVerdict:
    promise_type: str  # "hipaa_phi" | "groundedness" | "authoritative_source_cited"
    verdict: str  # "KEPT" | "BROKEN" | "NA"
    score: float | None  # 0.0-1.0 where applicable; None for NA
    evidence: str  # short human-readable reason (<=300 chars)


@dataclass
class PromiseKeptResult:
    overall: str  # "KEPT" | "BROKEN" | "NA"
    per_promise: list[PerPromiseVerdict] = field(default_factory=list)
    ruler_model: str = ""
    error: str | None = None
    error_transient: bool = False


@dataclass
class _GradeOutcome:
    """Internal: a verdict plus optional grader-failure detail, so
    grade_promise_kept can propagate the first failure to the
    top-level PromiseKeptResult.error / error_transient (§5) without
    the per-promise evidence string being the only record of it."""

    verdict: PerPromiseVerdict
    grader_error: str | None = None
    grader_error_transient: bool = False


def _resolved_ruler_model() -> str:
    """Best-effort resolve of the locked rag_eval_adjudicate model for
    the ``ruler_model`` field. Never raises — an unresolved ruler
    shouldn't block a verdict that's otherwise gradeable.

    rag_eval_adjudicate is locked to exactly one model in MODEL_ROSTER
    (see that ModelSpec's own comment — "appears in no other model's
    eligible_stages, so the bandit always resolves it deterministically").
    No router call needed; just find that model."""
    try:
        from app.services.model_registry import MODEL_ROSTER

        for model_id, spec in MODEL_ROSTER.items():
            if _RULER_STAGE in (spec.eligible_stages or []):
                return model_id
    except Exception:
        pass
    return "gemini-2.5-pro"  # documented lock (§6) — safe fallback name, not a live call


def _evidence(text: str, limit: int = 300) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _grade_hipaa_phi(*, output: str, hipaa_on: bool) -> _GradeOutcome:
    """§2a — SAFETY, hard gate, fail-closed.

    KNOWN v1 GAP (flagged to Eval, not silently assumed): the spec
    requires distinguishing "echoed disallowed PHI" (BROKEN) from
    "user-provided, minimum-necessary PHI" (KEPT) — see spec §2a. The
    PHI_CLASSIFIER_URL /classify contract observed in this codebase
    (app/main.py's _run_hipaa_gate_sync) returns gate/phi_flag/
    identifier_labels/evidence_categories — no signal distinguishing
    provenance (user-provided vs model-fabricated) or necessity. Until
    Eval confirms the classifier supports that distinction (or a
    different mechanism is specified), this v1 treats ANY classifier
    phi_flag on the output as BROKEN. This is intentionally the
    fail-closed direction (matches HIPAA discipline) but will
    over-flag legitimate necessary-PHI turns until resolved.
    """
    if not hipaa_on:
        return _GradeOutcome(PerPromiseVerdict("hipaa_phi", "NA", None, "hipaa_context not active this turn"))
    if not (output or "").strip():
        return _GradeOutcome(PerPromiseVerdict("hipaa_phi", "NA", None, "empty output — nothing to disclose"))

    phi_url = (os.environ.get("PHI_CLASSIFIER_URL") or "").rstrip("/")
    if not phi_url:
        # Fail-closed per §5: safety grader unavailable -> BROKEN, not NA.
        return _GradeOutcome(
            PerPromiseVerdict("hipaa_phi", "BROKEN", 0.0, "PHI_CLASSIFIER_URL not configured — fail-closed"),
            grader_error="PHI_CLASSIFIER_URL not configured",
            grader_error_transient=False,
        )

    try:
        import httpx

        async with httpx.AsyncClient(timeout=_PHI_CLASSIFY_TIMEOUT) as client:
            resp = await client.post(f"{phi_url}/classify", json={"text": output})
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:
        from app.communication.error_emit import classify_exception

        env = classify_exception(exc, tool="promise_kept_phi_classify")
        # §5: SAFETY grader failure -> fail-closed to BROKEN (not NA),
        # regardless of env.is_recoverable — a safety promise doesn't
        # get an NA-and-retry-later out. is_recoverable still drives
        # error_transient on the result, since that's about whether
        # the FAILURE is retryable, not whether the verdict is soft.
        return _GradeOutcome(
            PerPromiseVerdict(
                "hipaa_phi", "BROKEN", 0.0,
                f"PHI classifier call failed ({type(exc).__name__}) — fail-closed: "
                f"{_evidence(env.user_facing_message, 200)}",
            ),
            grader_error=f"hipaa_phi grader failed: {env.user_facing_message}",
            grader_error_transient=env.is_recoverable,
        )

    gate = str(body.get("gate") or "indeterminate").strip().lower()
    phi_flag = bool(body.get("phi_flag", True))

    if gate == "clean" and not phi_flag:
        return _GradeOutcome(PerPromiseVerdict("hipaa_phi", "KEPT", 1.0, "no PHI detected in output"))

    labels = body.get("identifier_labels") or body.get("evidence_categories") or []
    return _GradeOutcome(
        PerPromiseVerdict(
            "hipaa_phi", "BROKEN", 0.0,
            _evidence(f"classifier gate={gate} phi_flag={phi_flag} labels={labels}"),
        )
    )


async def _grade_groundedness(
    *,
    output: str,
    sources: list[dict] | None,
    adjudication_sub_scores: dict[str, float | None] | None,
) -> _GradeOutcome:
    """§2b — QUALITY, graded score.

    REVISED 2026-08-04 (Eval) — resolves the cross-service gap flagged
    2026-08-05: reuse the v2 post-run adjudicator's existing
    ``grounding`` sub-score (adjudication/full.py's compute_overall_score,
    stage ``adjudicator``, locked to gemini-2.5-pro per Task #25) rather
    than calling mobius-rag's check_facts — that's a separate deployed
    service with no HTTP endpoint exposing it, and it grades RAG's
    internal synthesis, a different pipeline point than the chat
    output. No cross-service call, no second grounder to drift.
    """
    if not (output or "").strip():
        return _GradeOutcome(PerPromiseVerdict("groundedness", "NA", None, "empty output — no claims to check"))
    if not sources:
        return _GradeOutcome(PerPromiseVerdict("groundedness", "NA", None, "no sources — can't ground"))

    if not adjudication_sub_scores:
        return _GradeOutcome(
            PerPromiseVerdict("groundedness", "NA", None, "adjudication did not run for this turn")
        )

    grounding = adjudication_sub_scores.get("grounding")
    if grounding is None:
        # §2b: dimension not active for this turn's category (e.g. a
        # non-RAG category where DIMENSION_CATEGORIES doesn't include
        # "grounding") -> NA, not BROKEN.
        return _GradeOutcome(
            PerPromiseVerdict("groundedness", "NA", None, "grounding dimension not active for this turn")
        )

    score = float(grounding)
    verdict = "KEPT" if score >= GROUNDEDNESS_KEPT_THRESHOLD else "BROKEN"
    return _GradeOutcome(
        PerPromiseVerdict(
            "groundedness", verdict, score,
            f"adjudicator grounding sub-score={score:.3f} (threshold={GROUNDEDNESS_KEPT_THRESHOLD})",
        )
    )


def _grade_authoritative_source_cited() -> _GradeOutcome:
    """§2c — blocked on Retriever's source-authority metadata. NA stub."""
    return _GradeOutcome(
        PerPromiseVerdict("authoritative_source_cited", "NA", None, "pending source-authority metadata")
    )


_PROMISE_BLOCK_KEY_TYPES = {
    "hipaa_context": "hipaa_phi",
    "grounding_promise": "groundedness",
    "product_promise": "authoritative_source_cited",
}


async def grade_promise_kept(
    *,
    output: str,
    active_promises: list[str],
    sources: list[dict] | None,
    hipaa_on: bool,
    adjudication_sub_scores: dict[str, float | None] | None = None,
    correlation_id: str | None = None,
) -> PromiseKeptResult:
    """Grade whether OUTPUT honored each active promise. See spec §1-5."""
    ruler_model = _resolved_ruler_model()

    if not active_promises:
        return PromiseKeptResult(overall="NA", per_promise=[], ruler_model=ruler_model)

    active_types = {
        _PROMISE_BLOCK_KEY_TYPES[bk] for bk in active_promises if bk in _PROMISE_BLOCK_KEY_TYPES
    }

    outcomes: list[_GradeOutcome] = []
    if "hipaa_phi" in active_types:
        outcomes.append(await _grade_hipaa_phi(output=output, hipaa_on=hipaa_on))
    if "groundedness" in active_types:
        outcomes.append(
            await _grade_groundedness(
                output=output, sources=sources, adjudication_sub_scores=adjudication_sub_scores,
            )
        )
    if "authoritative_source_cited" in active_types:
        outcomes.append(_grade_authoritative_source_cited())

    if not outcomes:
        return PromiseKeptResult(overall="NA", per_promise=[], ruler_model=ruler_model)

    per_promise = [o.verdict for o in outcomes]

    # §4: fail-closed combination.
    if any(v.verdict == "BROKEN" for v in per_promise):
        overall = "BROKEN"
    elif any(v.verdict == "KEPT" for v in per_promise):
        overall = "KEPT"
    else:
        overall = "NA"

    # Surface the first grader failure (if any) at the result level —
    # per-promise evidence strings carry the detail too, but error /
    # error_transient are the fields callers (bandit reward, badge UI)
    # are meant to check without parsing evidence text.
    first_failure = next((o for o in outcomes if o.grader_error), None)
    error = first_failure.grader_error if first_failure else None
    error_transient = first_failure.grader_error_transient if first_failure else False

    return PromiseKeptResult(
        overall=overall,
        per_promise=per_promise,
        ruler_model=ruler_model,
        error=error,
        error_transient=error_transient,
    )
