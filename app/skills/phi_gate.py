"""PHI gate for feedback capture — scrub-and-keep before free-text feedback
spreads to the classifier LLM, the DB, or task-manager.

Contract (PHI Classifier agent, docs/hipaa-phi-policy.md):
  1. /message-check(verbatim) → gate ∈ {clean, phi, indeterminate}.
  2. clean            → pass the raw text through.
  3. phi | indeterminate → /redact(verbatim):
       redaction ∈ {clean, masked} → use the REDACTED text downstream (scrubbed).
       redaction = suppressed OR /redact unreachable → DROP the text (keep only
                   category/metadata).
Fail-closed: any classifier error/timeout/non-200 is treated as PHI (never
silently pass unverified text). We scrub REGARDLESS of hipaa-mode for the
classify + promote paths (minimum-necessary); v1 also scrubs the stored verbatim
(conservative — safe in both modes).

§3 PHI-IN-LOGS: this module NEVER logs raw text — identifier labels + counts only.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PHI_URL = (
    os.environ.get("PHI_GATE_URL")
    or os.environ.get("PHI_CLASSIFIER_URL")
    or "https://mobius-phi-classifier-ortabkknqa-uc.a.run.app"
).rstrip("/")
_TIMEOUT = float(os.environ.get("PHI_GATE_TIMEOUT_SEC", "4"))
_DROP_MESSAGE = (
    "I couldn't store that — it looked like it might contain patient information. "
    "Could you rephrase without patient details? (Your category was still noted.)"
)


def _log_gate(where: str, body: dict) -> None:
    # labels + counts only — never the text or raw evidence
    logger.info(
        "[phi-feedback] %s gate=%s n=%s labels=%s ver=%s",
        where, body.get("gate"), body.get("identifiers_found"),
        body.get("identifier_labels"), body.get("classifier_version"),
    )


def gate_feedback_text(
    text: str, *, thread_id: str | None = None, user_id: str | None = None
) -> tuple[str | None, bool, bool]:
    """Return (safe_text, phi_scrubbed, dropped).

    - clean → (text, False, False)
    - scrubbed → (redacted_text, True, False)
    - dropped (suppressed / classifier down on redact) → (None, True, True)

    Callers MUST use safe_text (never the raw input) for classify/persist/promote,
    and skip capture of the text entirely when dropped.
    """
    if not (text or "").strip():
        return text, False, False

    import httpx

    gate = "indeterminate"  # fail-closed default
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_PHI_URL}/message-check",
                       json={"text": text, "thread_id": thread_id, "user_id": user_id})
        if r.status_code == 200:
            body = r.json()
            gate = (body.get("gate") or "indeterminate").strip().lower()
            _log_gate("check", body)
        else:
            logger.warning("[phi-feedback] message-check %s — fail-closed (treat as PHI)", r.status_code)
    except Exception as exc:  # network/timeout/parse
        logger.warning("[phi-feedback] message-check unreachable (%s) — fail-closed", type(exc).__name__)

    if gate == "clean":
        return text, False, False

    # phi or indeterminate → redact
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_PHI_URL}/redact", json={"text": text})
        if r.status_code == 200:
            body = r.json()
            _log_gate("redact", body)
            redaction = (body.get("redaction") or "suppressed").strip().lower()
            if redaction in ("clean", "masked"):
                return (body.get("redacted_text") or ""), True, False
            return None, True, True  # suppressed
        logger.warning("[phi-feedback] redact %s — dropping text (fail-closed)", r.status_code)
    except Exception as exc:
        logger.warning("[phi-feedback] redact unreachable (%s) — dropping text", type(exc).__name__)

    return None, True, True  # couldn't scrub safely → drop
