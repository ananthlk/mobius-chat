"""Per-user tool subscription persistence.

Stores and retrieves each user's opt-in/opt-out settings for individual
chat tools.  The pipeline calls ``get_allowed_tools_for_user`` at turn
start to resolve ``ctx.allowed_tools``; the settings API calls the
write helpers when the user toggles a tool in the UI.

Schema: ``user_tool_subscriptions`` (migration 035).

  user_id   TEXT  — auth user ID (matches chat_turns.user_id)
  tool_name TEXT  — canonical registry key, e.g. "search_corpus"
  enabled   BOOL  — True/False; absence of row = "use mode default"
  updated_at TIMESTAMPTZ

Semantics
---------
* A user with NO rows gets the full mode-appropriate tool list (i.e.
  the server-side default). This is intentional — new users don't need
  a bootstrap step.
* A row with ``enabled=True`` explicitly opts that tool back in (useful
  for power users who turned something off and changed their mind).
* A row with ``enabled=False`` blocks the tool regardless of mode.

``get_allowed_tools_for_user`` returns None when there are no
subscriptions for a user and no blocking rules, signaling "use all
tools from the mode default" to the orchestrator. This avoids the
overhead of fetching the full catalog on every turn.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from app.db_client import _err_code, _err_message, db_execute, db_query

logger = logging.getLogger(__name__)

_DB = "chat"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _env_is_hosted() -> bool:
    env = (os.environ.get("CHAT_ENV") or "dev").strip().lower()
    return env not in ("dev", "development", "local")


# ── Reads ─────────────────────────────────────────────────────────────────────


def get_user_tool_subscriptions(user_id: str) -> dict[str, bool]:
    """Return the user's explicit tool overrides as ``{tool_name: enabled}``.

    Returns an empty dict when:
      - the user has no rows (first-time user, no customisation yet), OR
      - the DB is unavailable (graceful degradation — caller gets the
        full tool list via the mode default path).

    Does NOT raise in any environment — tool policy reads are best-effort;
    a DB hiccup should not block the chat turn.
    """
    if not user_id:
        return {}
    result = db_query(
        "SELECT tool_name, enabled FROM user_tool_subscriptions WHERE user_id = :uid",
        _DB,
        params={"uid": user_id},
    )
    code = _err_code(result)
    if code == "relation_missing":
        # Migration 035 not yet applied — silently return empty (use mode defaults).
        return {}
    if code is not None:
        logger.warning("get_user_tool_subscriptions(%r) failed: %s", user_id[:8], _err_message(result))
        return {}
    cols = result.get("columns") or []
    rows = result.get("rows") or []
    out: dict[str, bool] = {}
    for row in rows:
        d = dict(zip(cols, row))
        out[str(d.get("tool_name") or "")] = bool(d.get("enabled"))
    return out


def get_allowed_tools_for_user(
    user_id: str | None,
    *,
    mode_defaults: list[str] | None = None,
) -> list[str] | None:
    """Resolve the final allowed-tool list for a user + mode combination.

    Args:
        user_id: Auth user ID. None → return None (use mode defaults without
            any per-user filtering).
        mode_defaults: If provided, this is the baseline tool list for the
            current chat mode. The user's subscriptions narrow/restore from
            this baseline. If None (meaning "all tools allowed"), user
            ``enabled=False`` rows block those specific tools from the full
            catalog; ``enabled=True`` rows are no-ops since everything is
            already allowed.

    Returns:
        ``None``  — no restrictions; use the full mode-appropriate tool list.
        ``[]``    — all tools blocked (e.g. task mode with no user overrides).
        ``[...]`` — explicit non-empty allow-list.

    The caller (orchestrator ``resolve_allowed_tools``) folds this result
    into ``ctx.allowed_tools``:
        - None → don't filter the manifest
        - [] or [x, y] → pass as ``allowed`` to ``get_tool_manifest``
    """
    if not user_id:
        return mode_defaults  # None means "all tools"

    subs = get_user_tool_subscriptions(user_id)
    if not subs and mode_defaults is None:
        # No user customisation AND no mode restriction → unrestricted.
        return None

    if mode_defaults is not None:
        # Start from mode baseline; apply user overrides.
        allowed = set(mode_defaults)
        for tool_name, enabled in subs.items():
            if enabled:
                allowed.add(tool_name)
            else:
                allowed.discard(tool_name)
        return sorted(allowed)

    # mode_defaults is None → all tools available as baseline.
    # Only user-disabled rows narrow the list.
    disabled = {t for t, en in subs.items() if not en}
    if not disabled:
        return None  # no restrictions at all
    # We need the full catalog to compute the allow-list.
    from app.skills.registry import skills_catalog
    all_tools = {row["name"] for row in skills_catalog()}
    return sorted(all_tools - disabled)


# ── Writes ────────────────────────────────────────────────────────────────────


def set_user_tool_subscription(user_id: str, tool_name: str, enabled: bool) -> None:
    """Upsert one tool subscription row for a user.

    Raises ``RuntimeError`` on unexpected DB errors in hosted envs.
    Silently logs in dev.
    """
    if not user_id:
        raise ValueError("user_id is required")
    if not tool_name or not tool_name.strip():
        raise ValueError("tool_name is required")
    result = db_execute(
        """
        INSERT INTO user_tool_subscriptions (user_id, tool_name, enabled, updated_at)
        VALUES (:uid, :tool, :enabled, now())
        ON CONFLICT (user_id, tool_name) DO UPDATE SET
            enabled    = EXCLUDED.enabled,
            updated_at = EXCLUDED.updated_at
        """,
        _DB,
        params={"uid": user_id, "tool": tool_name.strip(), "enabled": enabled},
    )
    code = _err_code(result)
    if code is None:
        return
    if code == "relation_missing":
        msg = "user_tool_subscriptions table missing — run migration 035"
        if _env_is_hosted():
            raise RuntimeError(msg)
        logger.warning(msg)
        return
    if code == "connection_error":
        msg = "DB unavailable; tool subscription not persisted"
        if _env_is_hosted():
            raise RuntimeError(msg)
        logger.warning(msg)
        return
    msg = f"set_user_tool_subscription failed: {_err_message(result)}"
    logger.error(msg)
    raise RuntimeError(msg)


def delete_user_tool_subscription(user_id: str, tool_name: str) -> None:
    """Delete a tool subscription row, restoring the mode default for that tool.

    Idempotent — no error if the row doesn't exist.
    """
    if not user_id or not tool_name:
        return
    result = db_execute(
        "DELETE FROM user_tool_subscriptions WHERE user_id = :uid AND tool_name = :tool",
        _DB,
        params={"uid": user_id, "tool": tool_name.strip()},
    )
    code = _err_code(result)
    if code is None:
        return
    if code in ("relation_missing", "connection_error"):
        logger.warning("delete_user_tool_subscription: %s", _err_message(result))
        return
    raise RuntimeError(f"delete_user_tool_subscription failed: {_err_message(result)}")


def reset_user_tool_subscriptions(user_id: str) -> None:
    """Delete ALL tool subscription rows for a user (full reset to mode defaults)."""
    if not user_id:
        return
    result = db_execute(
        "DELETE FROM user_tool_subscriptions WHERE user_id = :uid",
        _DB,
        params={"uid": user_id},
    )
    code = _err_code(result)
    if code is None:
        return
    if code in ("relation_missing", "connection_error"):
        logger.warning("reset_user_tool_subscriptions: %s", _err_message(result))
        return
    raise RuntimeError(f"reset_user_tool_subscriptions failed: {_err_message(result)}")
