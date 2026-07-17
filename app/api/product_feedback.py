"""Product-feedback endpoints (open feedback + satisfaction surveys).

Frontend-facing companions to the ``product_feedback`` skill. Where the skill is
invoked by the planner (inline / on-demand) and classifies via the standalone
service, these endpoints handle the UI-driven paths:

    POST /chat/product-feedback              submit an open feedback item (card)
    POST /chat/product-feedback/score        record a survey score (chip)
    POST /chat/product-feedback/event        log a funnel event (shown/dismissed/…)
    POST /chat/product-feedback/opt-out      stop / resume periodic asks

All go through ``require_user`` (same as the thumbs endpoints). Persistence:
``app.storage.product_feedback``. See docs/feedback-agent-spec.md.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.front_door import require_user
from app.storage import product_feedback as store

router = APIRouter(tags=["product_feedback"])

_CATEGORIES = set(store.ROUTING.keys())
_SURVEY_TYPES = set(store.SCORE_SCALES.keys())
_FOLLOWUP_PROMPT = "What's the main reason for your score?"

# Provenance tiers (docs/product-awareness-feedback-contract.md). Only genuinely
# user-VOICED triggers count in the human prompt→capture funnel and advance a
# user's periodic-ask cadence. Everything else is HARVESTED — the content may be
# user-authored (e.g. "graduation" = a new user's first typed question, or
# "auto_harvest" = a product_help miss) but the FILING is system-driven, so it
# must NOT log a funnel event or reset the user's feedback-nudge counters.
_USER_VOICED_TRIGGERS = {"inline", "on_demand", "periodic"}

# Only the auto-harvested doc-freshness signals may be closed over HTTP — never
# user feedback (bug/usability/…). Keeps the unauth drain endpoint narrow.
_SWEEPABLE = {"docs_gap", "doc_stale"}


class OpenFeedbackBody(BaseModel):
    verbatim: str
    category: str = "other"
    trigger: str = "on_demand"
    tidied: str | None = None
    summary: str | None = None
    sentiment: str = "neutral"
    severity: str = "low"
    area_tags: list[str] | None = None
    thread_id: str | None = None
    correlation_id: str | None = None
    parent_feedback_id: str | None = None
    # For agent-filed signals (doc_stale etc.) posted by external agents that
    # can't import app.storage: carries the SOURCE (agent name / git-hook id).
    # When set, it becomes the row's user_id (provenance) and the write is
    # treated as a non-user signal — user cadence is NOT advanced.
    source: str | None = None


class SurveyScoreBody(BaseModel):
    survey_type: str  # csat | ces | nps
    score: float
    trigger: str = "periodic"
    thread_id: str | None = None
    correlation_id: str | None = None


class EventBody(BaseModel):
    trigger: str
    action: str  # shown | opened | scored | submitted | dismissed | snoozed | opted_out
    kind: str | None = None
    category: str | None = None
    score: float | None = None
    feedback_id: str | None = None
    thread_id: str | None = None


class UpdateFeedbackBody(BaseModel):
    feedback_id: str | None = None   # or edit the latest open item in the thread
    thread_id: str | None = None
    category: str | None = None      # re-routes when changed
    tidied: str | None = None        # full-text replace (form edit)
    add_detail: str | None = None    # append (conversational "also…")
    sentiment: str | None = None
    severity: str | None = None


class CloseSignalsBody(BaseModel):
    category: str            # must be a sweepable signal (docs_gap | doc_stale)
    module: str              # required — a sweep only closes what it processed
    before: str | None = None  # ISO ts: only close signals it has actually seen
    source: str | None = None  # optional, for audit/attribution


class OptOutBody(BaseModel):
    opted_out: bool = True


@router.post("/chat/product-feedback")
def post_product_feedback(
    body: OpenFeedbackBody,
    user_id: str | None = Depends(require_user),
):
    """Persist an open feedback item submitted from the capture card."""
    if not (body.verbatim or "").strip():
        raise HTTPException(status_code=400, detail="verbatim is required")
    category = body.category if body.category in _CATEGORIES else "other"
    # Harvested (system-filed) vs. genuinely user-voiced feedback. Harvested =
    # a `source` is set OR the trigger isn't one of the user-voiced ones
    # (agent_signal, auto_harvest, graduation, …). Harvested writes skip the
    # user funnel + cadence; only user-voiced ones count.
    is_harvested = bool(body.source) or body.trigger not in _USER_VOICED_TRIGGERS
    row_user = body.source or user_id           # source wins for provenance
    fid = store.insert_open_feedback(
        trigger=body.trigger,
        category=category,
        verbatim=body.verbatim,
        tidied=body.tidied or body.verbatim,
        summary=body.summary or "",
        sentiment=body.sentiment,
        severity=body.severity,
        area_tags=body.area_tags or [],
        routed_to=store.route_for(category),
        user_id=row_user,
        thread_id=body.thread_id,
        correlation_id=body.correlation_id,
        parent_feedback_id=body.parent_feedback_id,
    )
    # The durable product_feedback row above is the record. The funnel event +
    # cadence advance are USER-only — harvested signals (agent_signal,
    # auto_harvest, graduation) aren't part of the human prompt→capture funnel
    # and must not touch a person's periodic-ask counters.
    if not is_harvested:
        store.log_event(trigger=body.trigger, action="submitted", user_id=user_id,
                        thread_id=body.thread_id, kind="open", category=category, feedback_id=fid)
        if user_id:
            store.mark_captured(user_id)
    return {"status": "ok", "feedback_id": fid, "category": category,
            "routed_to": store.route_for(category)}


@router.post("/chat/product-feedback/update")
def post_update_feedback(
    body: UpdateFeedbackBody,
    _user_id: str | None = Depends(require_user),
):
    """Edit an existing feedback item from the capture-card form (change category,
    rewrite text, or append). Re-routes when the category changes."""
    cat = body.category if body.category in _CATEGORIES else None
    upd = store.update_open_feedback(
        feedback_id=body.feedback_id, thread_id=body.thread_id, category=cat,
        tidied=body.tidied, add_detail=body.add_detail,
        sentiment=body.sentiment, severity=body.severity,
    )
    if not upd:
        raise HTTPException(status_code=404, detail="feedback item not found")
    return {"status": "ok", **upd}


@router.post("/chat/product-feedback/score")
def post_survey_score(
    body: SurveyScoreBody,
    user_id: str | None = Depends(require_user),
):
    """Record a one-tap survey score; return the optional follow-up prompt."""
    if body.survey_type not in _SURVEY_TYPES:
        raise HTTPException(status_code=400, detail="invalid survey_type")
    fid = store.insert_survey_score(
        survey_type=body.survey_type,
        score=body.score,
        trigger=body.trigger,
        user_id=user_id,
        thread_id=body.thread_id,
        correlation_id=body.correlation_id,
    )
    store.log_event(trigger=body.trigger, action="scored", user_id=user_id,
                    thread_id=body.thread_id, kind=body.survey_type, score=body.score,
                    feedback_id=fid)
    if user_id:
        store.mark_captured(user_id)
    return {"status": "ok", "feedback_id": fid, "followup_prompt": _FOLLOWUP_PROMPT}


@router.post("/chat/product-feedback/close-signals")
def post_close_signals(
    body: CloseSignalsBody,
    _user_id: str | None = Depends(require_user),
):
    """Drain covered doc-freshness signals after a sweep refreshes + re-embeds a
    doc — the HTTP counterpart to store.close_signals for external sessions (the
    weekly sweep) that can't import app.storage. Narrow by design: only sweepable
    signal categories, module required, optional `before` so a sweep closes only
    what it actually saw."""
    if body.category not in _SWEEPABLE:
        raise HTTPException(status_code=400,
                            detail=f"category must be one of {sorted(_SWEEPABLE)}")
    if not (body.module or "").strip():
        raise HTTPException(status_code=400, detail="module is required")
    ids = store.close_signals(category=body.category, module=body.module.strip(),
                              before=body.before)
    return {"status": "ok", "drained": len(ids), "feedback_ids": ids,
            "category": body.category, "module": body.module.strip()}


@router.post("/chat/product-feedback/event")
def post_feedback_event(
    body: EventBody,
    user_id: str | None = Depends(require_user),
):
    """Log a funnel event. A dismiss also snoozes; opt-out flips the flag."""
    store.log_event(trigger=body.trigger, action=body.action, user_id=user_id,
                    thread_id=body.thread_id, kind=body.kind, category=body.category,
                    score=body.score, feedback_id=body.feedback_id)
    if body.action == "dismissed" and user_id:
        store.snooze(user_id)
    elif body.action == "shown" and user_id:
        store.mark_prompted(user_id, kind=body.kind or "open")
    elif body.action == "opted_out" and user_id:
        store.set_opt_out(user_id, True)
    return {"status": "ok"}


@router.post("/chat/product-feedback/opt-out")
def post_opt_out(
    body: OptOutBody,
    user_id: str | None = Depends(require_user),
):
    """Stop (or resume) periodic feedback asks for this user."""
    if user_id:
        store.set_opt_out(user_id, body.opted_out)
    return {"status": "ok", "opted_out": body.opted_out}
