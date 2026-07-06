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
    "docs_gap": "a documentation gap",
    "doc_stale": "a stale doc",
}

# Clean nouns for the playback receipt's "Category:" line.
_CATEGORY_DISPLAY = {
    "accuracy_trust": "Accuracy / trust",
    "coverage_gap": "Coverage gap",
    "bug": "Bug",
    "speed": "Speed",
    "usability": "Usability",
    "feature_request": "Feature request",
    "praise": "Praise",
    "other": "Other",
    "docs_gap": "Docs gap",
    "doc_stale": "Doc stale",
}


def _open_receipt(category: str, tidied: str, tracked: bool, updated: bool = False) -> str:
    """The standard playback envelope: thanks + what-we-captured + edit invitation.
    Rendered as chat text today; the structured capture_card (in extra) lets a
    future frontend show the same thing as an editable card."""
    disp = _CATEGORY_DISPLAY.get(category, "Feedback")
    head = "✓ **Updated — thanks.**" if updated else "✓ **Feedback logged — thank you.**"
    lines = [head, "", f"- **Category:** {disp}",
             f'- **What I captured:** "{(tidied or "").strip()}"']
    if tracked:
        lines.append("- **Status:** flagged for follow-up")
    if not updated:
        lines += ["",
                  "Want to change anything? Just tell me — e.g. *“make it a bug”* or "
                  "*“add that it happens on mobile”* — and I’ll update it."]
    return "\n".join(lines)


def _survey_receipt(survey_type: str, score: float) -> str:
    s = int(score) if float(score).is_integer() else score
    if survey_type == "nps":
        return (f"✓ **Thanks — recorded your {s}/10.**\n\n"
                "What’s the main reason for your score? (optional — one line is plenty.)")
    return (f"✓ **Thanks — recorded {s}/5.**\n\n"
            "Anything you’d change to make it better? (optional.)")


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


# ── task promotion: page a human on page-worthy feedback ────────────────────
# Policy (owned here — the skill has the classification): promote a *named
# broken/wrong thing*, not any negative sentiment. bug/accuracy_trust always
# page; anything high-severity pages. praise/mild never pages. Transport +
# dedup + fire-and-forget live in app.services.task_manager_promotion.promote,
# gated by MOBIUS_TASK_MANAGER_PROMOTION. See the task-manager contract.
_PAGE_CATEGORIES = {"bug", "accuracy_trust"}


_NEVER_PAGE = {"praise"}  # positive feedback never pages, whatever the severity


def _promotion_severity(category: str, severity: str) -> str | None:
    """Map (category, severity) → task-manager severity, or None = don't page."""
    if category in _NEVER_PAGE:
        return None
    if category in _PAGE_CATEGORIES:
        return "critical" if severity == "high" else "warning"
    if severity == "high":
        return "warning"
    return None


def _maybe_promote_task(*, feedback_id, category, sentiment, severity, verbatim,
                        summary, correlation_id, thread_id, user_id, org_slug) -> None:
    """Best-effort: promote page-worthy feedback to a task. Never blocks or
    breaks the feedback write (promote() is itself daemon-threaded + flag-gated)."""
    task_severity = _promotion_severity(category, severity)
    if not task_severity or not feedback_id:
        return
    try:
        from app.services.task_manager_promotion import promote
        _headline = (summary or verbatim or "").strip()
        promote({
            "signal": "product_feedback",
            "correlation_id": correlation_id or "",
            # Stable per-item dedup key — one task row per feedback item, and
            # idempotent replays overwrite cleanly (fixes the all-collapse-to-one
            # bug where a missing correlation_id left source_ref NULL).
            "source_ref": f"feedback:{feedback_id}",
            "step_id": "product_feedback",
            "source_module": "feedback",
            "report_to_task_manager": True,
            "task_type": "blocker",
            "task_severity": task_severity,
            "issue_code": category,                       # bug | accuracy_trust | …
            "org_name": org_slug or "_shared_",           # sentinel when feedback has no org
            "title": f"Feedback ({category}): {_headline}"[:160],   # readable in a queue
            "note": (verbatim or summary or "")[:500],    # the user's own words
            "data": {
                "feedback_id": feedback_id,
                "category": category,
                "sentiment": sentiment,
                "severity": severity,
                "org_slug": org_slug,
                "verbatim": (verbatim or "")[:1000],
            },
            "thread_id": thread_id,
            "user_id": user_id,
        })
    except Exception:
        logger.warning("feedback task promotion failed — continuing", exc_info=True)


def _run_product_feedback(call: SkillCall) -> SkillEnvelope:
    inputs = call.inputs or {}
    trigger = inputs.get("trigger") or "on_demand"
    kind = (inputs.get("kind") or "open").lower()

    user_id = _ctx_field(call, "user_id")
    thread_id = call.thread_id or _ctx_field(call, "thread_id")
    correlation_id = _ctx_field(call, "correlation_id")
    org_slug = _ctx_field(call, "org_slug")
    config_sha = _ctx_field(call, "config_sha")

    # ── update path: the user edited/corrected feedback they just gave ──────
    if inputs.get("update"):
        new_cat = inputs.get("category")
        new_cat = new_cat if new_cat in _CATEGORY_LABEL else None
        upd = store.update_open_feedback(
            feedback_id=inputs.get("feedback_id"), thread_id=thread_id,
            category=new_cat, add_detail=inputs.get("add_detail"),
        )
        if not upd:
            return SkillEnvelope(text="I couldn't find that feedback to update — mind restating it?",
                                 signal="no_sources")
        cat = upd.get("category") or "other"
        tracked = upd.get("routed_to") in ("triage_queue", "corpus_backlog")
        return SkillEnvelope(
            text=_open_receipt(cat, upd.get("tidied") or "", tracked, updated=True),
            signal="no_sources",
            extra={"feedback_id": upd.get("feedback_id"), "kind": "open", "category": cat,
                   "capture_card": {"feedback_id": upd.get("feedback_id"), "category": cat,
                                    "categories": list(_CATEGORY_LABEL.keys()),
                                    "tidied": upd.get("tidied"), "editable": True}},
        )

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
            text=_survey_receipt(survey_type, score),
            signal="no_sources",
            extra={"feedback_id": fid, "kind": "survey", "survey_type": survey_type,
                   "score": score,
                   "capture_card": {"feedback_id": fid, "kind": "survey",
                                    "survey_type": survey_type, "score": score,
                                    "followup_prompt": "What's the main reason for your score?"}},
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

    # Page a human when the feedback names a broken/wrong thing (best-effort).
    _maybe_promote_task(
        feedback_id=fid, category=category, sentiment=c.get("sentiment") or "neutral",
        severity=c.get("severity") or "low", verbatim=verbatim,
        summary=c.get("summary") or "", correlation_id=correlation_id,
        thread_id=thread_id, user_id=user_id, org_slug=org_slug,
    )

    tracked = resp.get("routed_to") in ("triage_queue", "corpus_backlog")

    # Standard playback envelope (chat text today; capture_card in extra lets a
    # future frontend render the same thing as an editable card).
    return SkillEnvelope(
        text=_open_receipt(category, c.get("tidied") or verbatim, tracked),
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
                "mode": "confirm",           # pre-filled playback of captured feedback
                "update_url": "/chat/product-feedback/update",
            },
        },
    )


register(
    SkillSpec(
        name="product_feedback",
        description=(
            "Capture open product feedback about Mobius (a wish, complaint, bug, "
            "coverage gap, or PRAISE) or record a satisfaction survey score.\n"
            "Use when ANY of these hold — EVEN IF the message also contains a question:\n"
            "  • the user voices an opinion about the product: praise ('I love this', "
            "'this feature is great', 'I love Mobius'), a complaint ('the sidebar is "
            "confusing'), a wish ('I wish it could…'), or a gap ('you never have Ohio Medicaid');\n"
            "  • the user asks to give feedback, asks WHERE the feedback form is, or wants "
            "to report/suggest something ('looking for the feedback form', 'how do I give "
            "feedback', 'I want to report a bug') — this skill IS the feedback path "
            "(use trigger=on_demand).\n"
            "If the message ALSO asks a genuine question, still capture the feedback (you can "
            "answer the question in the same turn — praise is not a reason to skip logging it).\n"
            "Do NOT use for a PURE data/policy question with no opinion (e.g. 'what's the timely "
            "filing limit for Aetna') — that's search_corpus. Never rate clinical content.\n"
            "After recording, the skill returns a playback receipt that invites edits. If the "
            "user then corrects or adds to feedback they just gave ('actually it's a bug', "
            "'also it happens on mobile'), call again with update=true (+ category and/or "
            "add_detail) — it edits the same item, it does NOT create a new one.\n"
            "For a satisfaction survey (kind=survey), record the user's number as score."
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
                "update": {
                    "type": "boolean",
                    "description": "Edit the feedback the user just gave (don't create a new item). "
                                   "Pair with category and/or add_detail.",
                },
                "add_detail": {
                    "type": "string",
                    "description": "For update=true: extra detail to append to the existing item.",
                },
                "feedback_id": {
                    "type": "string",
                    "description": "For update=true: which item; defaults to the latest in this thread.",
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
