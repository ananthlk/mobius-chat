"""Builtin skill: product_feedback — open product feedback + satisfaction
surveys (CSAT/CES/NPS).

Complements the turn-scoped thumbs (chat_feedback). When the planner detects the
user voicing an opinion / wish / complaint about Mobius (inline), or the user
explicitly asks to give feedback (on-demand), it selects this tool. The skill
classifies the feedback via the standalone ``mobius-feedback`` service, persists
it chat-side (``app.storage.product_feedback``), routes it, and returns a short
acknowledgement plus a ``capture_card`` in ``extra`` the UI can optionally render.

Design: docs/feedback-agent-spec.md. Persistence lives here, not in the service,
so it reuses chat's proven db-agent connection (same as chat_feedback).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, register
from app.storage import product_feedback as store

logger = logging.getLogger(__name__)

FEEDBACK_SKILL_URL = os.environ.get(
    "CHAT_SKILLS_FEEDBACK_URL",
    "http://localhost:8060/classify",
).rstrip("/")
FEEDBACK_TIMEOUT_SEC = float(os.environ.get("CHAT_SKILLS_FEEDBACK_TIMEOUT_SEC", "10"))

_CATEGORY_LABEL = {
    "accuracy_trust": "accuracy",
    "coverage_gap": "coverage",
    "bug": "a bug",
    "speed": "speed",
    "usability": "usability",
    "feature_request": "a feature request",
    "praise": "praise",
    "other": "feedback",
}


def _classify(verbatim: str, context_excerpt: str, provisional: str | None, cid: str | None) -> dict:
    """Call the stateless classifier service; degrade to a best-effort local
    classification if it's unavailable so feedback is never lost."""
    payload = {
        "verbatim": verbatim,
        "context_excerpt": context_excerpt or None,
        "provisional_category": provisional,
        "correlation_id": cid,
    }
    try:
        req = urllib.request.Request(
            FEEDBACK_SKILL_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=FEEDBACK_TIMEOUT_SEC) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logger.warning("[product_feedback] classify service failed: %s", e)
        cat = provisional if provisional in _CATEGORY_LABEL else "other"
        return {
            "classification": {
                "category": cat, "sentiment": "neutral", "severity": "low",
                "summary": verbatim[:160], "tidied": verbatim[:600],
            },
            "routed_to": "product_backlog",
            "reason": "service_unavailable",
        }


def _ctx_field(call: SkillCall, name: str):
    ctx = call.pipeline_ctx
    return getattr(ctx, name, None) if ctx is not None else None


def _run_product_feedback(call: SkillCall) -> SkillEnvelope:
    inputs = call.inputs or {}
    trigger = inputs.get("trigger") or "on_demand"
    kind = (inputs.get("kind") or "open").lower()

    user_id = _ctx_field(call, "user_id")
    thread_id = call.thread_id or _ctx_field(call, "thread_id")
    correlation_id = _ctx_field(call, "correlation_id")
    org_slug = _ctx_field(call, "org_slug")
    config_sha = _ctx_field(call, "config_sha")

    # ── survey score path (uncommon via tool; usually posted from the chip) ──
    if kind == "survey":
        survey_type = (inputs.get("survey_type") or "csat").lower()
        try:
            score = float(inputs.get("score"))
        except (TypeError, ValueError):
            return SkillEnvelope(text="", signal="no_sources")
        fid = store.insert_survey_score(
            survey_type=survey_type, score=score, trigger=trigger,
            user_id=user_id, thread_id=thread_id, correlation_id=correlation_id,
            org_slug=org_slug,
        )
        store.log_event(trigger=trigger, action="scored", user_id=user_id,
                        thread_id=thread_id, kind=survey_type, score=score, feedback_id=fid)
        store.mark_captured(user_id) if user_id else None
        return SkillEnvelope(
            text="Thanks — that's recorded.",
            signal="no_sources",
            extra={"feedback_id": fid, "kind": "survey", "survey_type": survey_type,
                   "score": score},
        )

    # ── open feedback path ──────────────────────────────────────────────────
    verbatim = (inputs.get("verbatim") or call.user_message or call.question or "").strip()
    if not verbatim:
        return SkillEnvelope(text="", signal="no_sources")

    resp = _classify(
        verbatim=verbatim,
        context_excerpt=inputs.get("context_excerpt") or "",
        provisional=inputs.get("category"),
        cid=correlation_id,
    )
    c = resp.get("classification") or {}
    category = c.get("category") or "other"
    area_tags = inputs.get("area_tags") if isinstance(inputs.get("area_tags"), list) else []

    fid = store.insert_open_feedback(
        trigger=trigger,
        category=category,
        verbatim=verbatim,
        tidied=c.get("tidied") or verbatim,
        summary=c.get("summary") or "",
        sentiment=c.get("sentiment") or "neutral",
        severity=c.get("severity") or "low",
        area_tags=area_tags,
        routed_to=resp.get("routed_to"),
        user_id=user_id, thread_id=thread_id, correlation_id=correlation_id,
        org_slug=org_slug, config_sha=config_sha,
        parent_feedback_id=inputs.get("parent_feedback_id"),
    )
    store.log_event(trigger=trigger, action="submitted", user_id=user_id,
                    thread_id=thread_id, kind="open", category=category, feedback_id=fid)
    if user_id:
        store.mark_captured(user_id)

    label = _CATEGORY_LABEL.get(category, "feedback")
    tracked = resp.get("routed_to") in ("triage_queue", "corpus_backlog")
    ack = f"Logged — filed under {label}." + (" We'll track it." if tracked else " Thanks for flagging it.")

    # capture_card lets the UI optionally show an editable confirmation; the ack
    # text alone already delivers the skill without any frontend change.
    return SkillEnvelope(
        text=ack,
        signal="no_sources",
        extra={
            "feedback_id": fid,
            "kind": "open",
            "category": category,
            "capture_card": {
                "feedback_id": fid,
                "category": category,
                "categories": list(_CATEGORY_LABEL.keys()),
                "sentiment": c.get("sentiment") or "neutral",
                "tidied": c.get("tidied") or verbatim,
                "editable": True,
            },
        },
    )


register(
    SkillSpec(
        name="product_feedback",
        description=(
            "Capture open product feedback about Mobius (a wish, complaint, bug, "
            "coverage gap, or praise) or record a satisfaction survey score.\n"
            "Use when: the user voices an opinion / suggestion / complaint about the "
            "product itself (not a data question) — e.g. 'I wish it could…', 'the "
            "sidebar is confusing', 'you never have Ohio Medicaid', 'this is great'; "
            "OR the user explicitly asks to give feedback.\n"
            "Do NOT use when: the user is asking a substantive question, needs data, or "
            "wants documentation — that's not feedback. Never rate clinical content. "
            "Returns a short acknowledgement; the feedback is persisted and routed."
        ),
        inputs_schema={
            "type": "object",
            "properties": {
                "trigger": {
                    "type": "string",
                    "enum": ["inline", "on_demand", "periodic"],
                    "description": "How the feedback was initiated.",
                },
                "verbatim": {
                    "type": "string",
                    "description": "The user's feedback in their own words. Defaults to the user message.",
                },
                "category": {
                    "type": "string",
                    "enum": [
                        "accuracy_trust", "coverage_gap", "bug", "speed",
                        "usability", "feature_request", "praise", "other",
                    ],
                    "description": "Optional provisional category; the classifier may override.",
                },
                "context_excerpt": {
                    "type": "string",
                    "description": "Optional recent-turns excerpt for tone/intent.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["open", "survey"],
                    "description": "open = free-text feedback (default); survey = a numeric score.",
                },
                "survey_type": {
                    "type": "string",
                    "enum": ["csat", "ces", "nps"],
                    "description": "For kind=survey.",
                },
                "score": {
                    "type": "number",
                    "description": "For kind=survey: the numeric score.",
                },
            },
        },
        handler=_run_product_feedback,
        requires_jurisdiction=False,
        follow_up_capable=True,
        visible_to_planner=True,
        category="utility",
        display_name="Share feedback",
    )
)
