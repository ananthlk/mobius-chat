"""User-profile splicing helpers.

Mobius-user/CONSUMER_RECIPE_PROFILE.md is the contract. This module
implements the chat-side consumer:

  * ``splice_user_profile(base, profile)`` — Pattern A from the recipe.
    Drop ``profile.rendered_prompt`` between a base system prompt and
    whatever follows. No-op when the user hasn't onboarded.
  * ``autonomy_for(profile, sensitive)`` — Pattern B. Read-only access
    to ``profile.autonomy.{routine,sensitive}_tasks`` for tool-execution
    gating (confirm-first vs auto). Defaults to ``confirm_first`` when
    profile is missing — safest fallback.
  * ``personalization_emit_payload(profile)`` — compact dict for the
    ``personalization_applied`` envelope chat emits at preflight so the
    user sees their preferences are being honored on every turn.

Splicing is applied at five LLM-bearing stages:
  - planner / ReAct reasoning system prompts (autonomy + tasks drive
    tool selection and confirmation gating)
  - critic system prompt (validates that the answer matches user's
    preference shape — tone, experience level, directness)
  - integrator / consolidator system prompt (the user-facing voice;
    where tone + ai_experience_level matter most)
  - adjudicator system prompt (post-run quality check now graded
    against user's preferences in addition to grounding)

Token cost: rendered_prompt is 4-6 lines, ~150 tokens. Across 5 stages
that's ~750 tokens per turn — acceptable at flash/haiku rates,
visible at Opus. Toggle via ``CHAT_PERSONALIZATION_ENABLED=0`` for
A/B comparison or fallback.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    """Master kill switch. Default ON. Set
    ``CHAT_PERSONALIZATION_ENABLED=0`` to disable across all stages —
    useful for A/B baseline + emergency rollback."""
    raw = (os.environ.get("CHAT_PERSONALIZATION_ENABLED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def splice_user_profile(base_prompt: str, profile: dict | None) -> str:
    """Pattern A — splice ``profile.rendered_prompt`` into a system prompt.

    Returns ``base_prompt`` unchanged when:
      - personalization disabled via env (``CHAT_PERSONALIZATION_ENABLED=0``)
      - profile is None (un-onboarded user)
      - profile.rendered_prompt missing or empty
      - profile is not a dict (defensive)

    These all map to "no preferences known — use base prompt only" per
    the recipe's first-run UX guidance.
    """
    if not _enabled():
        return base_prompt
    if not profile or not isinstance(profile, dict):
        return base_prompt
    rendered = (profile.get("rendered_prompt") or "")
    if not isinstance(rendered, str):
        return base_prompt
    rendered = rendered.strip()
    if not rendered:
        return base_prompt
    return f"{base_prompt}\n\n{rendered}"


def autonomy_for(profile: dict | None, *, sensitive: bool) -> str:
    """Pattern B — return autonomy mode for routine vs sensitive tasks.

    Used by the planner and tool-execution gates to decide whether to
    confirm-before-execute, dry-run-only, or auto-execute. Recipe-spec
    return values: ``"automatic" | "confirm_first" | "manual"``.

    Defaults to ``confirm_first`` when the profile isn't available —
    safest behavior absent explicit user consent. The planner reads
    this independent of personalization being enabled (the structured
    field is always trustworthy if present, even when we're A/B-ing
    rendered_prompt off).
    """
    if not profile or not isinstance(profile, dict):
        return "confirm_first"
    auto = profile.get("autonomy")
    if not isinstance(auto, dict):
        return "confirm_first"
    key = "sensitive_tasks" if sensitive else "routine_tasks"
    val = (auto.get(key) or "").strip().lower()
    if val in ("automatic", "confirm_first", "manual"):
        return val
    return "confirm_first"


def personalization_emit_payload(profile: dict | None) -> dict[str, Any]:
    """Build the compact dict that lands in the ``personalization_applied``
    envelope. Visible on every turn's thinking_log so the user sees
    their preferences are being honored — and ops gets a debuggable
    fingerprint per turn.

    When personalization is disabled or profile is missing, returns
    ``{"applied": False, "reason": ...}`` so the envelope still emits
    (visibility into the negative case matters too).
    """
    if not _enabled():
        return {"applied": False, "reason": "feature_disabled_via_env"}
    if not profile or not isinstance(profile, dict):
        return {"applied": False, "reason": "no_profile"}
    auto = profile.get("autonomy") if isinstance(profile.get("autonomy"), dict) else {}
    comm = profile.get("communication") if isinstance(profile.get("communication"), dict) else {}
    rendered = profile.get("rendered_prompt") or ""
    rendered_len = len(rendered) if isinstance(rendered, str) else 0
    tasks = profile.get("tasks") if isinstance(profile.get("tasks"), list) else []
    return {
        "applied": True,
        "preferred_name": profile.get("preferred_name"),
        "tone": comm.get("tone"),
        "ai_experience_level": comm.get("ai_experience_level"),
        "greeting_enabled": comm.get("greeting_enabled"),
        "autonomy_routine": auto.get("routine_tasks"),
        "autonomy_sensitive": auto.get("sensitive_tasks"),
        "task_count": len(tasks),
        "rendered_prompt_chars": rendered_len,
        "version": profile.get("version"),
    }
