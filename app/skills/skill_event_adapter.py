"""Translator: mobius-skills-core ``SkillEvent`` → chat's ``EmitEnvelope``.

The shared skill package (``mobius_skills_core``) defines a minimal
``SkillEvent`` shape that is consumer-neutral. Chat's ``EmitEnvelope``
is richer — it carries correlation_id, thread_id, user_id, a
round number, source_module, plus a ``report_to_task_manager`` flag
with typed severity that the task-manager promotion writer reads.

This adapter converts one to the other. Wire it into a skill call by
building a closure that captures the pipeline context and forwards
each SkillEvent through ``on_thinking``:

    from app.skills.skill_event_adapter import make_skill_emitter
    from mobius_skills_core.skills.google_search import run_google_search

    emitter = make_skill_emitter(
        on_thinking=on_thinking,         # the orchestrator's emitter
        correlation_id=ctx.correlation_id,
        thread_id=ctx.thread_id,
        user_id=ctx.user_id,
        round=current_round,
    )
    result = run_google_search(query, emitter=emitter)

The adapter itself is stateless — each ``SkillEvent`` → one
``EmitEnvelope`` dict handed to ``on_thinking``. No buffering, no
filtering, no re-ordering.

Design notes
------------
* The skill's ``task_type`` / ``task_severity`` suggestions are copied
  through verbatim; the chat's promotion writer reads those and
  decides. If the skill says ``task_type="blocker"`` the writer
  promotes; if the skill says ``None`` it doesn't. Promotion policy
  lives entirely in the event — this file just translates.

* ``report_to_task_manager`` is set to ``True`` whenever the skill
  suggested a task_type. Consumers that want to fan out to task-manager
  inherit that opt-in from the skill; ones that ignore promotion
  (e.g. tests) never see the flag matter.

* The ``source_module`` in the envelope is still ``"chat"`` — it
  indicates which service emitted the envelope, not which package
  implemented the skill. If an external MCP client wrapped the same
  skill it would set ``source_module="mcp"``. This preserves analytics
  that group events by their emit surface.
"""
from __future__ import annotations

from typing import Any, Callable

from mobius_skills_core import SkillEvent


def make_skill_emitter(
    on_thinking: Callable[[Any], None],
    *,
    correlation_id: str,
    thread_id: str | None = None,
    user_id: str | None = None,
    round: int | None = None,
) -> Callable[[SkillEvent], None]:
    """Build a SkillEvent callback bound to this call's context.

    Args:
        on_thinking: The orchestrator's emit channel. Takes either a
            string (legacy) or an envelope dict (new). We always feed
            it the envelope dict shape.
        correlation_id: From ``ctx.correlation_id``. Required.
        thread_id: From ``ctx.thread_id``. Pass through even when None
            (dev-mode threads without thread_id are valid).
        user_id: From ``ctx.user_id``. None in dev / auth=off mode.
        round: The ReAct loop round number when available, else None.

    Returns:
        A one-arg callable ``(SkillEvent) -> None`` suitable for passing
        as the ``emitter=`` kwarg on any skill in ``mobius_skills_core``.

    The returned callable is synchronous and never raises — if
    ``on_thinking`` itself raises, the exception bubbles up to the skill
    which swallows it via ``_safe_emit`` on the core side. Net effect:
    a faulty ``on_thinking`` pipeline can't take down a skill mid-call.
    """

    def _emit(event: SkillEvent) -> None:
        # Translate to the EmitEnvelope shape (as a plain dict — chat's
        # on_thinking accepts dicts per the Sprint A.1 migration).
        envelope: dict[str, Any] = {
            "signal": event.signal,
            "correlation_id": correlation_id,
            "step_id": event.step_id,
            "data": dict(event.data or {}),
            "timestamp_ms": event.ts_ms,
            "source_module": "chat",
            "report_to_task_manager": bool(event.task_type),
        }
        if event.note:
            envelope["note"] = event.note
        if thread_id:
            envelope["thread_id"] = thread_id
        if user_id:
            envelope["user_id"] = user_id
        if round is not None:
            envelope["round"] = round
        if event.task_type:
            envelope["task_type"] = event.task_type
        if event.task_severity:
            envelope["task_severity"] = event.task_severity
        on_thinking(envelope)

    return _emit
