"""Sprint A.2 — promote agentic emit envelopes to task-manager.

Sprint A.1 established the envelope shape; this module implements the
writer that consumes ``envelope.report_to_task_manager`` and POSTs
matching events to the task-manager skill for analysis.

**Architectural decision** (from 2026-04-19 debate): events are tasks.
A critic_flagged, a tool_exhausted, a rounds_exhausted_with_warning —
these are all things the chat PM analyzes the same way they analyze
a credentialing blocker. Same substrate, same UI, same query surface.
Not a separate agent_events table.

**Promotion channel:** POST /tasks/signal on the task-manager skill
(port 8015, existing endpoint). Task-manager's TaskSignalBody already
accepts all the fields we want to populate — we're just supplying a
``source_module="chat"`` and letting task-manager's enrichment layer
turn our envelopes into TaskCards.

**Failure mode:** fire-and-forget. If task-manager is down, slow, or
returns 5xx, we log WARNING and the chat turn continues. Promoted
events lost in transit won't come back (no retry queue yet — if this
becomes a concern, Sprint A.3 can add a Postgres outbox table).

**Gating:** ``MOBIUS_TASK_MANAGER_PROMOTION`` controls the writer.
Default ON (2026-04-20) after Sprint A.2 soak — operators set ``=0``
to disable. Fire-and-forget failure mode means the worst case of an
unreachable task-manager is a log warning per turn, not broken chat.

**Volume expectation:** the 10 promoted signals fire roughly 1-3 times
per chat turn on average. For a system at 100 turns/day, that's
100-300 rows/day in task-manager. Well within the existing tasks
table's write capacity.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)


# ── Feature flag ─────────────────────────────────────────────────────


def promotion_enabled() -> bool:
    """Read at call time (not module load) so .env changes don't
    require a worker restart for this specific knob.

    Default **ON** as of 2026-04-20 — Sprint A.2 soak completed and
    task-manager promotion is the expected production behavior. Set
    ``MOBIUS_TASK_MANAGER_PROMOTION=0`` (or ``false``/``no``/``off``)
    to disable explicitly.
    """
    raw = (os.environ.get("MOBIUS_TASK_MANAGER_PROMOTION") or "").strip().lower()
    if raw == "":
        return True  # default ON
    return raw not in ("0", "false", "no", "off")


# ── Writer ──────────────────────────────────────────────────────────


# Short timeout — fire-and-forget. If task-manager is slow, we don't
# want to add latency to every chat emit. Long enough to tolerate a
# normal network round-trip; short enough that a degraded task-manager
# doesn't stall the worker.
_PROMOTION_HTTP_TIMEOUT_S = 3.0


def _task_manager_base_url() -> str:
    """Local resolver to avoid a circular import on app.api._common.
    Matches that module's logic."""
    return (os.environ.get("CHAT_SKILLS_TASK_MANAGER_URL") or "http://localhost:8015").rstrip("/")


def _build_signal_body(envelope: dict[str, Any]) -> dict[str, Any]:
    """Convert a chat emit envelope dict into a task-manager
    ``TaskSignalBody``-shaped payload.

    Field mapping:
      envelope.signal          → body.signal
      envelope.step_id         → body.step_id
      envelope.data            → body.data
      envelope.note            → body.note
      envelope.task_type       → body.type (required by task-manager)
      envelope.task_severity   → body.severity
      envelope.correlation_id  → body.source_ref ("correlation_id:<id>")
      envelope.thread_id       → body.data["thread_id"]
      envelope.user_id         → body.data["user_id"]

    Task-manager's enrichment layer is responsible for producing a
    human-readable title/body from these fields. We supply the
    structured data; task-manager renders the card.
    """
    data = dict(envelope.get("data") or {})
    # Cross-reference fields that task-manager's enrichment can use
    # to build a "back to chat thread" link.
    tid = envelope.get("thread_id")
    if tid:
        data["thread_id"] = tid
    uid = envelope.get("user_id")
    if uid:
        data["user_id"] = uid
    rnd = envelope.get("round")
    if rnd is not None:
        data["round"] = rnd

    # Prefer an EXPLICIT source_ref (e.g. "feedback:<id>") — it's the stable dedup
    # key. Fall back to the per-turn correlation_id only when the caller didn't
    # supply one (backward-compatible; other emit sites are unaffected).
    cid = envelope.get("correlation_id") or ""
    source_ref = envelope.get("source_ref") or (f"correlation_id:{cid}" if cid else None)

    return {
        "signal": envelope.get("signal") or "note",
        "step_id": envelope.get("step_id") or "",
        "type": envelope.get("task_type") or "info",
        "severity": envelope.get("task_severity") or "low",
        "source_module": envelope.get("source_module") or "chat",
        "source_ref": source_ref,
        "org_name": envelope.get("org_name") or envelope.get("org") or "_shared_",
        # Forward optional overrides when present (None = task-manager derives it,
        # unchanged for callers that don't set them).
        "title": envelope.get("title") or None,
        "issue_code": envelope.get("issue_code") or None,
        "data": data,
        "note": envelope.get("note") or "",
        "workflow": "chat",
        "created_by": "system",
    }


def _post_signal_sync(payload: dict[str, Any]) -> None:
    """Blocking POST. Short timeout, swallow any errors — promotion
    is best-effort analytics, not the chat turn's critical path."""
    import httpx

    url = f"{_task_manager_base_url()}/tasks/signal"
    try:
        with httpx.Client(timeout=_PROMOTION_HTTP_TIMEOUT_S) as client:
            resp = client.post(url, json=payload)
            if resp.status_code >= 400:
                logger.warning(
                    "task-manager promotion: %s returned %d: %s",
                    payload.get("signal"),
                    resp.status_code,
                    (resp.text or "")[:200],
                )
            else:
                logger.info(
                    "task-manager promotion: %s (signal=%s, type=%s)",
                    resp.status_code,
                    payload.get("signal"),
                    payload.get("type"),
                )
    except Exception as e:
        # Every failure path ends here: network timeout, DNS failure,
        # connection refused, bad JSON, whatever. Log and move on.
        logger.warning(
            "task-manager promotion failed (signal=%s): %s",
            payload.get("signal"),
            e,
        )


def promote(envelope: dict[str, Any]) -> None:
    """Promote one envelope to task-manager if its flag is set.

    Call sites pass the envelope dict (``EmitEnvelope.to_dict()``
    output). We check ``report_to_task_manager`` and fire only when
    the flag is True AND the feature is enabled at runtime.

    Runs on a background thread so the calling emit site doesn't
    block on HTTP. ``_PROMOTION_HTTP_TIMEOUT_S`` caps the worst case.
    """
    if not isinstance(envelope, dict):
        return
    if not envelope.get("report_to_task_manager"):
        return
    if not promotion_enabled():
        return

    payload = _build_signal_body(envelope)

    # Background thread so even a slow task-manager doesn't delay the
    # chat pipeline. If the worker exits before the thread completes,
    # the promotion drops — acceptable for analytics-grade data.
    thread = threading.Thread(
        target=_post_signal_sync,
        args=(payload,),
        daemon=True,
        name=f"tm-promote-{payload.get('signal', '?')[:20]}",
    )
    thread.start()
