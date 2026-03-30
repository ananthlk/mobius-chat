"""Why the credentialing orchestrator advanced: copilot validate vs autopilot policy vs skip.

Emitted to chat and stored on run state (``gate_events``) for UI and debugging.
"""

from __future__ import annotations

import os
from typing import Any, Literal

ReasonCode = Literal[
    "copilot_user_validated",
    "copilot_step_completed_awaiting_validation",
    "autopilot_policy_advance",
    "autopilot_awaiting_confirmation",
    "step_skipped_prerequisite",
    "step_failed",
]

MAX_GATE_EVENTS = 40


def get_credentialing_prerequisites_status() -> dict[str, Any]:
    """What must be set for co-pilot / autopilot runs to work end-to-end."""

    chat_db = bool(
        (os.environ.get("CHAT_RAG_DATABASE_URL") or "").strip()
        or (os.environ.get("RAG_DATABASE_URL") or "").strip()
        or (os.environ.get("CHAT_DATABASE_URL") or "").strip()
    )
    roster_url = bool((os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").strip())
    redis_url = bool((os.environ.get("REDIS_URL") or "").strip())

    recommendations: list[str] = []
    if not roster_url:
        recommendations.append(
            "Set CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL so org/location/provider steps can call the skill API."
        )
    if not chat_db:
        recommendations.append(
            "Set CHAT_RAG_DATABASE_URL (or RAG_DATABASE_URL) so co-pilot runs persist across worker/API "
            "and credentialing_assertion / roster_review can save."
        )
    if not redis_url:
        recommendations.append(
            "Set REDIS_URL if you use the async chat worker (queue); optional for single-process dev."
        )

    return {
        "chat_database_configured": chat_db,
        "provider_roster_url_configured": roster_url,
        "redis_configured": redis_url,
        "ready_for_credentialing_api": roster_url,
        "ready_for_persisted_copilot_runs": chat_db and roster_url,
        "recommendations": recommendations,
    }


def append_gate_event(state: Any, event: dict[str, Any]) -> None:
    events: list[dict[str, Any]] = getattr(state, "gate_events", None) or []
    if not isinstance(events, list):
        events = []
    events.append(event)
    if len(events) > MAX_GATE_EVENTS:
        events = events[-MAX_GATE_EVENTS:]
    setattr(state, "gate_events", events)


def build_user_validated_event(
    step_id: str,
    *,
    org_name: str = "",
) -> dict[str, Any]:
    return {
        "kind": "validate",
        "step_id": step_id,
        "reason_code": "copilot_user_validated",
        "run_mode": "copilot",
        "detail": "User submitted validated_output for this step; advancing to the next step.",
        "org_name": (org_name or "").strip(),
    }


def build_step_completed_event(
    *,
    step_id: str,
    org_name: str,
    run_mode: str,
    step_status: str,
    step_summary: str,
    last_active_roster_cutoff: int | None = None,
    autopilot_force_confirm: bool = False,
    extra_detail: str | None = None,
) -> dict[str, Any]:
    mode = (run_mode or "copilot").strip().lower()
    if mode not in ("copilot", "autopilot"):
        mode = "copilot"

    if step_status == "skipped":
        return {
            "kind": "step_end",
            "step_id": step_id,
            "reason_code": "step_skipped_prerequisite",
            "run_mode": mode,
            "detail": (step_summary or "Prerequisite not met; step skipped.").strip(),
            "org_name": (org_name or "").strip(),
        }

    if step_status == "done" and mode == "copilot":
        return {
            "kind": "step_end",
            "step_id": step_id,
            "reason_code": "copilot_step_completed_awaiting_validation",
            "run_mode": mode,
            "detail": "Step finished; co-pilot waits for you to confirm or edit the draft before continuing.",
            "org_name": (org_name or "").strip(),
        }

    if step_status == "done" and mode == "autopilot":
        if autopilot_force_confirm:
            return {
                "kind": "step_end",
                "step_id": step_id,
                "reason_code": "autopilot_awaiting_confirmation",
                "run_mode": mode,
                "detail": (extra_detail or "Autopilot could not commit under current policy; human confirmation required.").strip(),
                "org_name": (org_name or "").strip(),
            }
        parts = [
            "Autopilot advanced: step completed under machine policy (scores/registry/cutoff as configured)."
        ]
        if step_id == "find_associated_providers" and last_active_roster_cutoff is not None:
            parts.append(f" Active panel cutoff used: {last_active_roster_cutoff}/100.")
        if extra_detail:
            parts.append(f" {extra_detail}")
        return {
            "kind": "step_end",
            "step_id": step_id,
            "reason_code": "autopilot_policy_advance",
            "run_mode": mode,
            "detail": "".join(parts).strip(),
            "org_name": (org_name or "").strip(),
            "active_roster_cutoff": last_active_roster_cutoff,
        }

    return {
        "kind": "step_end",
        "step_id": step_id,
        "reason_code": "step_failed",
        "run_mode": mode,
        "detail": (step_summary or "Step did not complete normally.").strip(),
        "org_name": (org_name or "").strip(),
    }


def gate_event_emit_line(ev: dict[str, Any]) -> str:
    code = ev.get("reason_code") or ""
    sid = ev.get("step_id") or ""
    short = {
        "copilot_user_validated": "you confirmed the prior step",
        "copilot_step_completed_awaiting_validation": "co-pilot pauses for your review",
        "autopilot_policy_advance": "autopilot met promotion policy and continued",
        "autopilot_awaiting_confirmation": "autopilot needs your confirmation on this gate",
        "step_skipped_prerequisite": "step skipped (missing prerequisite)",
        "step_failed": "step incomplete or failed",
    }.get(str(code), str(code))
    return f"◌ Credentialing gate — {sid}: {short}. {ev.get('detail') or ''}".strip()


def emit_gate_event(emitter: Any, ev: dict[str, Any]) -> None:
    if not emitter or not ev:
        return
    try:
        emitter(gate_event_emit_line(ev))
    except Exception:
        pass
