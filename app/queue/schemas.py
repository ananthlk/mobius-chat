"""Canonical payload shapes for queue request and response. All modules use these."""
from typing import Any

# Request: what gets written to the queue when a chat question is submitted.
# API (or any client) publishes this; worker consumes it.
REQUEST_PAYLOAD_KEYS = ("message", "session_id")  # optional: session_id

# Response: what the worker publishes back. API (or client) reads by correlation_id.
# Keys: status, message, plan, thinking_log, error (optional)
RESPONSE_STATUS_PENDING = "pending"
RESPONSE_STATUS_COMPLETED = "completed"
RESPONSE_STATUS_FAILED = "failed"


def make_request_payload(message: str, session_id: str | None = None) -> dict[str, Any]:
    """Build payload for publish_request. Worker receives this."""
    out: dict[str, Any] = {"message": message or ""}
    if session_id is not None:
        out["session_id"] = session_id
    return out


def make_response_payload(
    status: str,
    message: str | None = None,
    plan: dict | None = None,
    thinking_log: list[str] | None = None,
    error: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build payload for publish_response. API returns this when polling."""
    out: dict[str, Any] = {"status": status}
    if message is not None:
        out["message"] = message
    if plan is not None:
        out["plan"] = plan
    if thinking_log is not None:
        out["thinking_log"] = thinking_log
    if error is not None:
        out["error"] = error
    out.update(extra)
    return out
