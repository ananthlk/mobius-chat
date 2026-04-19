"""Feedback + QC endpoints (Phase 1b).

Routes:
    POST /chat/qc-audit/{correlation_id}
        Merge QC / eval adjudication into the live response + turn row
        + progress stream. Guarded by ``MOBIUS_QC_AUDIT_SECRET`` header.
    POST /chat/qc-user-score/{correlation_id}
        Persist a human-override quality score (0–1 + optional comment)
        into ``chat_turns.qc_audit``.
    POST /chat/adjudication-feedback/{correlation_id}
        Thumbs + comment on the adjudicator / QA scorecard (technical users).
    POST /chat/feedback/{correlation_id}
        Turn-level thumbs up/down + optional comment.
    POST /chat/llm-performance-feedback/{correlation_id}
        Model-routing / efficiency thumbs (separate from answer-quality).
    POST /chat/source-feedback/{correlation_id}
        Per-source thumbs.

Extracted from ``app/main.py`` as Phase 1b of the main-split refactor.
External URLs preserved via ``app.include_router``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.api.front_door import require_user
from app.queue import get_queue
from app.storage import (
    fetch_turn_qc_audit,
    insert_adjudication_feedback,
    insert_feedback,
    insert_llm_performance_feedback,
    insert_source_feedback,
)
from app.storage.progress import publish_quality_audit_event
from app.storage.turns import update_turn_qc_audit

router = APIRouter(tags=["feedback"])

# Phase 2d: the five "user thumbs / score" endpoints below go through
# ``require_user``. ``/chat/qc-audit`` stays on its own
# ``MOBIUS_QC_AUDIT_SECRET`` service-to-service check — it's invoked
# by the eval adjudicator, not by a browser client, so user auth
# doesn't apply.


# ── Request bodies ─────────────────────────────────────────────────────────


class FeedbackBody(BaseModel):
    rating: str  # "up" | "down"
    comment: str | None = None


class QcAuditBody(BaseModel):
    passed: bool
    reason: str | None = None
    source: str = "eval_adjudicator"
    score: float | None = None  # automated 0–1; defaults from passed if omitted
    sub_scores: dict[str, float] | None = None
    adjudicator_full_response: str | None = None
    adjudicator_model: str | None = None
    adjudicator_llm_call_id: str | None = None


class QcUserScoreBody(BaseModel):
    """Human override for adjudicator score; merged into chat_turns.qc_audit JSON."""

    user_score: float
    user_score_comment: str | None = None


class AdjudicationFeedbackBody(BaseModel):
    rating: str
    comment: str | None = None


class SourceFeedbackBody(BaseModel):
    source_index: int  # 1-based
    rating: str  # "up" | "down"


class LlmPerformanceFeedbackBody(BaseModel):
    rating: str
    comment: str | None = None


# ── QC audit / human override ──────────────────────────────────────────────


@router.post("/chat/qc-audit/{correlation_id}")
def post_chat_qc_audit(
    correlation_id: str,
    body: QcAuditBody,
    x_mobius_qc_audit_secret: str | None = Header(None, alias="X-Mobius-QC-Audit-Secret"),
):
    """Merge QC / eval adjudication into the live response, turn row, and progress stream (thinking)."""
    secret = (os.environ.get("MOBIUS_QC_AUDIT_SECRET") or "").strip()
    if secret and (x_mobius_qc_audit_secret or "").strip() != secret:
        raise HTTPException(status_code=403, detail="Invalid or missing QC audit secret")

    audited_at = datetime.now(timezone.utc).isoformat()
    src = (body.source or "eval_adjudicator").strip()[:200] or "eval_adjudicator"
    reason_str = (body.reason or "").strip()[:2000]
    auto_score = body.score
    if auto_score is not None:
        auto_score = max(0.0, min(1.0, float(auto_score)))
    else:
        auto_score = 1.0 if body.passed else 0.0
    qc_dict: dict[str, Any] = {
        "passed": body.passed,
        "reason": reason_str,
        "source": src,
        "audited_at": audited_at,
        "automated_score": round(auto_score, 4),
    }
    if body.sub_scores:
        cleaned: dict[str, float] = {}
        for k, v in body.sub_scores.items():
            ks = str(k).strip()[:120]
            if not ks:
                continue
            try:
                fv = float(v)
                cleaned[ks] = round(max(0.0, min(1.0, fv)), 4)
            except (TypeError, ValueError):
                pass
        if cleaned:
            qc_dict["sub_scores"] = cleaned
    if body.adjudicator_full_response and str(body.adjudicator_full_response).strip():
        qc_dict["adjudicator_full_response"] = str(body.adjudicator_full_response).strip()[:8000]
    if body.adjudicator_model and str(body.adjudicator_model).strip():
        qc_dict["adjudicator_model"] = str(body.adjudicator_model).strip()[:200]
    if body.adjudicator_llm_call_id and str(body.adjudicator_llm_call_id).strip():
        qc_dict["adjudicator_llm_call_id"] = str(body.adjudicator_llm_call_id).strip()[:120]
    sym = "✓" if body.passed else "⚠"
    label = "passed" if body.passed else "flagged"
    reason_bit = f" — {reason_str[:180]}" if reason_str else ""
    line = f"{sym} Quality audit {label}{reason_bit}"
    update_turn_qc_audit(correlation_id, qc_dict)
    full_qc = fetch_turn_qc_audit(correlation_id) or qc_dict
    publish_quality_audit_event(
        correlation_id,
        {"passed": body.passed, "source": src},
        line,
    )
    get_queue().patch_response_merge(
        correlation_id,
        {"qc_audit": full_qc, "thinking_log": [line]},
    )
    return {"status": "ok", "qc_audit": full_qc}


@router.post("/chat/qc-user-score/{correlation_id}")
def post_qc_user_score(
    correlation_id: str,
    body: QcUserScoreBody,
    _user_id: str | None = Depends(require_user),
):
    """Persist edited quality score (0–1) + optional note into qc_audit; patches live response for poll/SSE."""
    if body.user_score < 0.0 or body.user_score > 1.0:
        raise HTTPException(status_code=400, detail="user_score must be between 0 and 1")

    merge = {
        "user_score": round(float(body.user_score), 4),
        "user_score_comment": (body.user_score_comment or "").strip()[:2000] or None,
        "user_score_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    update_turn_qc_audit(correlation_id, merge)
    full = fetch_turn_qc_audit(correlation_id)
    if isinstance(full, dict) and full:
        get_queue().patch_response_merge(correlation_id, {"qc_audit": full})
    return {"status": "ok", "qc_audit": full or merge}


# ── Feedback (thumbs) ──────────────────────────────────────────────────────


@router.post("/chat/adjudication-feedback/{correlation_id}")
def post_adjudication_feedback_route(
    correlation_id: str,
    body: AdjudicationFeedbackBody,
    _user_id: str | None = Depends(require_user),
):
    """Thumbs + comment on the adjudicator / QA scorecard (technical users)."""
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    insert_adjudication_feedback(correlation_id, body.rating, body.comment or None)
    return {"status": "ok"}


@router.post("/chat/feedback/{correlation_id}")
def post_chat_feedback(
    correlation_id: str,
    body: FeedbackBody,
    _user_id: str | None = Depends(require_user),
):
    """Persist turn-level feedback (thumbs up/down + optional comment)."""
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    insert_feedback(correlation_id, body.rating, body.comment or None)
    return {"status": "ok"}


@router.post("/chat/llm-performance-feedback/{correlation_id}")
def post_llm_performance_feedback(
    correlation_id: str,
    body: LlmPerformanceFeedbackBody,
    _user_id: str | None = Depends(require_user),
):
    """Model routing / efficiency feedback (separate from answer-quality thumbs)."""
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    insert_llm_performance_feedback(correlation_id, body.rating, body.comment or None)
    return {"status": "ok"}


@router.post("/chat/source-feedback/{correlation_id}")
def post_chat_source_feedback(
    correlation_id: str,
    body: SourceFeedbackBody,
    _user_id: str | None = Depends(require_user),
):
    """Persist per-source feedback (thumbs up/down)."""
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    if body.source_index < 1:
        raise HTTPException(status_code=400, detail="source_index must be >= 1")
    insert_source_feedback(correlation_id, body.source_index, body.rating)
    return {"status": "ok"}
