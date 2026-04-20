"""HIPAA PHI audit trail writer.

Writes to ``phi_audit_log`` (see db/schema/020_llm_analytics.sql) every
time the pipeline detects PHI in a user message or an LLM output.
Required for HIPAA audit-trail compliance: "who/what/when/where/how" of
every PHI encounter, regardless of whether the request succeeded or was
blocked.

2026-04-20 hardening: the table existed for months but had zero
writers. This module is the single source of truth for audit writes.

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
    Writes are fire-and-forget via the db-agent. Errors log at debug
    — we must NEVER break the main pipeline because PHI audit log
    write failed. Missing audit entries are an ops issue; they are
    not worse than blocking a user's clinical request.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from app.db_client import db_execute, err_code, err_message

logger = logging.getLogger(__name__)


# PHI detection patterns — matches (loosely) what adjudication/full.py
# already looks for, extended to cover DOB + MRN. Extensible; ops can
# add patterns here and they flow to every caller.
_PHI_PATTERNS: dict[str, re.Pattern[str]] = {
    "ssn":            re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b"),
    "member_id":      re.compile(r"member\s*(?:id|#|number)\s*[:#]?\s*\S+", re.I),
    "patient_name":   re.compile(r"patient\s*(?:name)?\s*[:]\s*[A-Z][a-z]+", re.I),
    "dob":            re.compile(r"\b(?:dob|date of birth)\b\s*[:]?\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", re.I),
    "mrn":            re.compile(r"\b(?:mrn|medical record)\s*[:#]?\s*[A-Z0-9-]+", re.I),
    # 9-digit run that isn't SSN-formatted (covers un-separated SSNs + member ids)
    "9digit_id":      re.compile(r"\b\d{9}\b"),
}


def detect_phi(text: str) -> tuple[list[str], int]:
    """Scan ``text`` and return (list of PHI type labels, total match count).

    Used by callers that want to decide whether to write an audit row
    and what ``phi_types`` to record.
    """
    if not text:
        return [], 0
    hits: list[str] = []
    total = 0
    for label, pat in _PHI_PATTERNS.items():
        matches = pat.findall(text)
        if matches:
            hits.append(label)
            total += len(matches)
    return hits, total


def _hipaa_mode_active() -> bool:
    """True when the process was started in HIPAA-restricted mode.

    Ops flips ``CHAT_HIPAA_MODE=1`` to narrow model rotation to HIPAA-
    eligible providers. Defaults to False to preserve dev ergonomics.
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

    Never raises. Logs at debug on failure so the main pipeline is
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
    """Convenience wrapper: detect PHI in ``text`` and write an audit
    event if any pattern matched. Returns True if an audit row was
    written, False otherwise.

    Callers that want to branch on "is there PHI here" should use
    ``detect_phi()`` directly; this one-call helper is for stages
    that just want to log on detection and move on.
    """
    types, count = detect_phi(text or "")
    if not types:
        return False
    write_phi_audit_event(
        correlation_id=correlation_id,
        thread_id=thread_id,
        event_type=event_type,
        phi_types=types,
        phi_count=count,
        stage=stage,
        model_used=model_used,
        action_taken=action_taken,
    )
    return True
