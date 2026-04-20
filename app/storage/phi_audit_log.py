"""HIPAA PHI audit trail writer — chat-side host for the shared skill.

Writes to ``phi_audit_log`` (see db/schema/020_llm_analytics.sql) every
time the pipeline detects PHI in a user message or an LLM output.
Required for HIPAA audit-trail compliance: "who/what/when/where/how"
of every PHI encounter, regardless of whether the request succeeded
or was blocked.

2026-04-20 — detection moved to ``mobius_skills_core.skills.phi_audit``
so credentialing, roster, and future surfaces share the same pattern
library and emit contract. This module keeps:

  * The DB writer (``phi_audit_log`` is a chat-owned table — other
    products have their own audit stores).
  * The host-side env reads (``CHAT_HIPAA_MODE``, ``CHAT_BAA_*``) that
    compute ``hipaa_mode_active`` / ``baa_available`` before handing
    them to the skill.
  * The ``audit_if_phi`` convenience that stages call with a single
    line to "detect + log if present".

Detection callers in other repos (credentialing workflow events,
roster-upload pipelines, future agents) should import
``run_phi_audit`` or ``detect_phi`` directly from skills-core; they
do not need this module.

Shape (mirrors the table schema):
    event_id           — auto gen_random_uuid()
    ts                 — auto NOW()
    correlation_id     — chat turn id (links audit to llm_calls, chat_turns)
    thread_id          — chat thread id (per-conversation drill-down)
    event_type         — 'request_phi_detected' | 'response_phi_detected' |
                         'llm_blocked_non_hipaa_model' | 'manual_review'
    phi_types          — comma-separated labels ('ssn', 'member_id',
                         'patient_name', 'dob', 'mrn')
    phi_count          — how many distinct patterns matched
    stage              — which stage detected it ('plan', 'resolve',
                         'integrate', 'adjudicate', 'front_door')
    model_used         — model id that processed / would have processed
    action_taken       — 'allowed'   — HIPAA-eligible model, processed
                         'blocked'   — non-HIPAA model, request refused
                         'redacted'  — PHI stripped before LLM call
                         'logged_only' — no action (detection-only)
    hipaa_mode_active  — True when CHAT_HIPAA_MODE=1 at the time
    baa_available      — True when a BAA is signed with the active model's vendor

Failure mode:
    Writes are fire-and-forget via the db-agent. Errors log at warning
    — we must NEVER break the main pipeline because PHI audit log
    write failed. Missing audit entries are an ops issue; they are
    not worse than blocking a user's clinical request.
"""
from __future__ import annotations

import logging
import os

from app.db_client import db_execute, err_code, err_message

# Detection is now a shared skill — chat is one of several hosts.
from mobius_skills_core.skills.phi_audit import (
    detect_phi as _skill_detect_phi,
    run_phi_audit,
)

logger = logging.getLogger(__name__)


# ── Re-export the skill's detector for existing chat callers ─────────
#
# Anything in chat that used to do ``from app.storage.phi_audit_log
# import detect_phi`` keeps working — we just delegate to the skill
# so there's one source of regex truth.

def detect_phi(text: str) -> tuple[list[str], int]:
    """Scan ``text`` for PHI — delegates to the skills-core detector.

    Thin wrapper preserved for call-site stability. Prefer importing
    from ``mobius_skills_core.skills.phi_audit`` directly in new code.
    """
    return _skill_detect_phi(text)


# ── Host-specific env lookups (not in the skill) ─────────────────────


def _hipaa_mode_active() -> bool:
    """True when the process was started in HIPAA-restricted mode.

    Ops flips ``CHAT_HIPAA_MODE=1`` to narrow model rotation to HIPAA-
    eligible providers. Defaults to False to preserve dev ergonomics.

    Env-reading lives here (not in the skill) because other hosts may
    derive this flag differently — credentialing, for example, may
    treat every request as HIPAA-mode regardless of env.
    """
    return (os.environ.get("CHAT_HIPAA_MODE") or "").strip().lower() in {"1", "true", "yes"}


def _baa_available_for(model: str | None) -> bool:
    """Best-effort check: does the active model's provider have a BAA?

    Today this is a static list — extend when the vendor contracts list
    changes. False on empty / unknown model so the audit row surfaces
    the ambiguity rather than asserting something we can't prove.
    """
    if not model:
        return False
    m = model.lower()
    if m.startswith("claude"):
        # Anthropic Enterprise has BAA availability — set to True if
        # your org has signed one. Leave False until confirmed.
        return (os.environ.get("CHAT_BAA_ANTHROPIC") or "").strip().lower() in {"1", "true"}
    if m.startswith("gemini") or "vertex" in m:
        return (os.environ.get("CHAT_BAA_GOOGLE") or "").strip().lower() in {"1", "true"}
    if m.startswith("gpt") or m.startswith("openai"):
        return (os.environ.get("CHAT_BAA_OPENAI") or "").strip().lower() in {"1", "true"}
    return False


# ── DB writer (chat-specific) ────────────────────────────────────────


def write_phi_audit_event(
    *,
    correlation_id: str | None,
    thread_id: str | None,
    event_type: str,
    phi_types: list[str] | None = None,
    phi_count: int = 0,
    stage: str | None = None,
    model_used: str | None = None,
    action_taken: str = "logged_only",
) -> None:
    """Insert one row into ``phi_audit_log``. Fire-and-forget.

    Never raises. Logs at warning on failure so the main pipeline is
    never blocked by an audit-write issue.
    """
    types_str = ",".join(phi_types or [])
    result = db_execute(
        "INSERT INTO phi_audit_log ("
        "correlation_id, thread_id, event_type, phi_types, phi_count, "
        "stage, model_used, action_taken, hipaa_mode_active, baa_available"
        ") VALUES ("
        ":cid, :tid, :event_type, :phi_types, :phi_count, "
        ":stage, :model, :action, :hipaa, :baa"
        ")",
        "chat",
        params={
            "cid": correlation_id,
            "tid": thread_id,
            "event_type": event_type,
            "phi_types": types_str,
            "phi_count": int(phi_count),
            "stage": stage,
            "model": model_used,
            "action": action_taken,
            "hipaa": _hipaa_mode_active(),
            "baa": _baa_available_for(model_used),
        },
    )
    code = err_code(result)
    if code is None:
        return
    # Silent failure is deliberate — never let a PHI audit write break
    # the user's turn. But log loudly enough for ops.
    logger.warning(
        "phi_audit_log write failed code=%s msg=%s "
        "(cid=%s event=%s action=%s)",
        code, err_message(result), correlation_id, event_type, action_taken,
    )


# ── Convenience helper for chat stages ───────────────────────────────


def audit_if_phi(
    text: str,
    *,
    correlation_id: str | None,
    thread_id: str | None,
    event_type: str,
    stage: str | None = None,
    model_used: str | None = None,
    action_taken: str = "logged_only",
) -> bool:
    """One-liner for chat stages: detect + write if PHI present.

    Internally routes through the shared skill (for detection +
    structured emit) and then writes the chat-specific audit row via
    ``write_phi_audit_event`` if anything was found.

    Returns True if an audit row was written, False otherwise.

    Callers that want to branch on "is there PHI here" without writing
    should use ``detect_phi()`` (or the skill directly). This helper
    is for stages that just want to log on detection and move on.
    """
    try:
        result = run_phi_audit(
            text or "",
            event_type=event_type,
            correlation_id=correlation_id,
            thread_id=thread_id,
            stage=stage,
            model_used=model_used,
            action_taken=action_taken,
            hipaa_mode_active=_hipaa_mode_active(),
            baa_available=_baa_available_for(model_used),
        )
    except Exception as e:  # defensive — must never break caller
        logger.warning("phi_audit skill raised: %s", e)
        return False

    if not result.extra.get("detected"):
        return False

    write_phi_audit_event(
        correlation_id=correlation_id,
        thread_id=thread_id,
        event_type=event_type,
        phi_types=result.extra["phi_types"],
        phi_count=result.extra["phi_count"],
        stage=stage,
        model_used=model_used,
        action_taken=action_taken,
    )
    return True
