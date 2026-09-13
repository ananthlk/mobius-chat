"""Task-manager HTTP transport — the seam between chat and the service.

Extracted from ``app/skills/builtin/tasks.py`` 2026-09-12 (Ananth: put the
task-manager tools in a separate module). Nothing here knows what a task IS;
it knows how to reach the service at ``CHAT_SKILLS_TASK_MANAGER_URL`` and what
to return when that service is absent.

Separated because this is the only part that crosses a process boundary. The
handlers beside it are chat logic; this is the contract with
``mobius-skills/task-manager``, and the two fail for different reasons and are
owned by different people.

KNOWN GAP, recorded here rather than in a comment nobody reads: ``_task_base()``
falls back to ``http://localhost:8015`` when the env var is UNSET, and
``config.py:228``'s guard only fires on a wrongly-SET value
(``bool(tm_url) and "localhost" in tm_url``). So an unset variable in a hosted
environment silently points every task tool at localhost, with no warning. Set
in ``deploy/dev.env`` and ``deploy.sh`` today, so latent. Raised to Ananth.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, register
from app.skills.task_envelope import TaskEnvelope, TaskRow

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT_S = 10.0


# ── HTTP helpers (stdlib-only, mirrors app/sub_skills/task_management.py) ──


def _task_base() -> str:
    """Resolve task-manager base URL, lowercased + trimmed of trailing slash."""
    return (
        os.environ.get("CHAT_SKILLS_TASK_MANAGER_URL") or "http://localhost:8015"
    ).rstrip("/")


def _is_stub_url(url: str) -> bool:
    """True when the configured URL is a placeholder, not a real endpoint.

    Matches the legacy ``react_loop`` checks so dev environments that
    deliberately set the URL to a sentinel get the same friendly message.
    """
    if not url:
        return True
    return ".invalid" in url or "not-yet-deployed" in url


def _http_request(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def _http_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET with stdlib urlencode — keeps the dependency surface tiny."""
    from urllib.parse import urlencode
    qs = urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    url = f"{_task_base()}{path}"
    if qs:
        url = f"{url}?{qs}"
    return _http_request("GET", url)


def _stub_envelope(operation: str) -> SkillEnvelope:
    """Friendly fallback used when the task-manager URL points at a
    placeholder. Same wording the legacy branch returned so dev users
    don't see a regression."""
    messages = {
        "list": "The task manager is coming soon. Tasks will appear here once the service is live.",
        "create": (
            "Task noted! The task manager is coming soon — "
            "your manager will be notified through the usual channel in the meantime."
        ),
        "resolve": "The task manager is coming soon. Task resolution will be available once the service is live.",
    }
    return SkillEnvelope(
        text=messages.get(operation, "The task manager is coming soon."),
        signal="corpus_only",
    )


def _emit(call: SkillCall, msg: str) -> None:
    if call.emitter:
        try:
            call.emitter(msg)
        except Exception:  # pragma: no cover — emitter is best-effort
            pass


def _attach_to_ctx(call: SkillCall, envelope: TaskEnvelope) -> None:
    """Write the structured payload to ``pipeline_ctx.react_task_list_data``.

    ``app/stages/integrate.py`` reads this attribute and injects a
    ``task_list`` UI block — the same path the legacy inline branch
    used. Setting the attribute is a no-op when the dispatcher didn't
    pass a pipeline context (e.g. an MCP caller invoking the skill
    standalone)."""
    ctx = call.pipeline_ctx
    if ctx is None:
        return
    try:
        ctx.react_task_list_data = envelope.to_react_payload()
    except Exception as e:  # pragma: no cover — context is loose-typed
        logger.debug("attach react_task_list_data failed (non-fatal): %s", e)


