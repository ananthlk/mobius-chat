"""Builtin skill: vibe — short, work-adjacent vibe lines (toasts, empathy,
dry observations, gratitude). Thin wrapper that forwards to the standalone
``mobius-skills/vibe`` Cloud Run service.

Use cases (planner-driven and agent-tool):
- User says something casual/tired ("ugh, this workflow", "long day").
- A hard task just completed (toast).
- The thread has been long and the user seems frustrated (empathy).
- User explicitly asks for something light ("tell me something fun").

The standalone service handles model selection (cheap+fast via the chat LLM
router's ``badge`` stage), prompt voice, and post-generation policy filtering.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, register

logger = logging.getLogger(__name__)


VIBE_SKILL_URL = os.environ.get(
    "CHAT_SKILLS_VIBE_URL",
    "http://localhost:8050/vibe",
).rstrip("/")

VIBE_TIMEOUT_SEC = float(os.environ.get("CHAT_SKILLS_VIBE_TIMEOUT_SEC", "10"))


def _run_vibe(call: SkillCall) -> SkillEnvelope:
    inputs = call.inputs or {}
    payload = {
        "trigger": inputs.get("trigger") or "user_initiated",
        "mode_hint": inputs.get("mode_hint"),
        "excerpt": inputs.get("excerpt") or (call.question or "")[:800],
        "position": inputs.get("position") or "standalone",
    }

    try:
        req = urllib.request.Request(
            VIBE_SKILL_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=VIBE_TIMEOUT_SEC) as r:
            resp = json.loads(r.read().decode())
    except Exception as e:
        logger.warning("[vibe] service call failed: %s", e)
        return SkillEnvelope(text="", signal="no_sources")

    line = (resp.get("line") or "").strip()
    if resp.get("skipped") or not line:
        return SkillEnvelope(text="", signal="no_sources")

    return SkillEnvelope(
        text=line,
        signal="no_sources",
        extra={
            "vibe_mode": resp.get("mode"),
            "vibe_position": resp.get("position"),
        },
    )


register(
    SkillSpec(
        name="vibe",
        description=(
            "Short, work-adjacent vibe line for the chat (toast, empathy, gratitude, "
            "dry observation, self-deprecating humor, or a data joke).\n"
            "Use when: user message is casual/tired/celebratory and not asking a substantive "
            "question; a hard task just completed; the thread shows fatigue cues; or the user "
            "explicitly asks for something light.\n"
            "Do NOT use when: user is asking a real question, needs data, or wants documentation. "
            "Voice is dry and healthcare-ops-aware. Returns one sentence (≤15 words) or empty if "
            "nothing fits. Never about patients, clinical topics, or politics."
        ),
        inputs_schema={
            "type": "object",
            "properties": {
                "trigger": {
                    "type": "string",
                    "enum": [
                        "user_initiated", "user_casual", "user_tired",
                        "long_thread", "task_done", "error_recovery", "clarifying_info",
                    ],
                    "description": "What cued the vibe call.",
                },
                "mode_hint": {
                    "type": "string",
                    "enum": [
                        "self_deprecating", "data_joke", "gratitude",
                        "toast", "empathy", "dry_observation",
                    ],
                    "description": "Suggested mode; service may override.",
                },
                "excerpt": {
                    "type": "string",
                    "description": "Optional last-turn or user-message excerpt for context.",
                },
                "position": {
                    "type": "string",
                    "enum": ["opener", "closer", "standalone"],
                    "description": "Where the line will be spliced.",
                },
            },
        },
        handler=_run_vibe,
        requires_jurisdiction=False,
        follow_up_capable=True,
        visible_to_planner=True,
    )
)
