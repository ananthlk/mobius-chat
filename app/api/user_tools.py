"""User tool subscription API — settings page for the chat UI.

Three routes that let a user manage their personal tool policy:

    GET  /user/tools                — fetch catalog + per-user subscription state
    PUT  /user/tools/{tool_name}    — enable or disable a single tool
    DELETE /user/tools/{tool_name}  — reset one tool to mode default
    POST /user/tools/reset          — reset ALL tools to mode defaults

The catalog is built from the skill registry (``registry.skills_catalog()``)
which includes both registry-registered skills and the router-owned builtins
(search_corpus, healthcare_npi_lookup, etc.) as synthetic entries. The UI
groups by the ``category`` field on each entry.

Auth: requires a valid JWT (``require_user`` dependency). Anonymous access
to tool subscriptions is rejected with 401.  ``require_user`` is wired to
``CHAT_AUTH_MODE`` so dev-without-auth still works (user_id = None → 401 in
hosted, but also gracefully handled here).

Why a separate module (not in chat.py):
  - These endpoints are user-profile data, not chat lifecycle data.
  - They'll eventually live alongside other ``/user/*`` endpoints
    (profile, preferences, history) in a dedicated user-settings router.
  - Different auth posture: auth is mandatory here regardless of CHAT_AUTH_MODE
    because writes are always user-scoped. (Currently we still go through
    require_user for consistency; the stricter gate can be added in a follow-up.)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.front_door import require_user
from app.skills import registry

logger = logging.getLogger(__name__)

router = APIRouter(tags=["user-tools"])


# ── Response / request models ────────────────────────────────────────────────


class ToolEntry(BaseModel):
    """One tool in the catalog, with the user's current subscription state."""

    name: str
    display_name: str
    description: str
    category: str
    source: str  # "builtin" | "mcp"
    visible_to_planner: bool
    # Subscription state — None means "no explicit setting; uses mode default"
    enabled: bool | None = None


class ToolsResponse(BaseModel):
    """Full catalog grouped by category with subscription state overlaid."""

    tools: list[ToolEntry]
    categories: list[str]  # ordered unique categories


class ToolSubscriptionRequest(BaseModel):
    enabled: bool
    """True = user opts in; False = user opts out."""


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/user/tools", response_model=ToolsResponse)
def get_user_tools(user_id: str | None = Depends(require_user)) -> dict[str, Any]:
    """Return the full tool catalog with the user's subscription state overlaid.

    Each entry has ``enabled: null`` when the user hasn't customised that
    tool (meaning "use mode default"). The UI renders null as the default
    toggle state for the current mode.

    Authentication: user_id is required. Returns 401 when auth is disabled
    (CHAT_AUTH_MODE=off) and no user_id is available, since tool subscriptions
    are always user-scoped.
    """
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required for tool settings")

    # Fetch subscription state
    try:
        from app.storage.tool_policy import get_user_tool_subscriptions
        subs = get_user_tool_subscriptions(user_id)
    except Exception as exc:
        logger.warning("get_user_tools: subscription fetch failed: %s", exc)
        subs = {}

    # Build catalog with subscription state
    catalog = registry.skills_catalog()
    entries: list[ToolEntry] = []
    seen_categories: list[str] = []
    category_order: list[str] = []

    for row in catalog:
        name = row["name"]
        cat = row["category"]
        if cat not in category_order:
            category_order.append(cat)
        entries.append(ToolEntry(
            name=name,
            display_name=row["display_name"],
            description=row["description"],
            category=cat,
            source=row["source"],
            visible_to_planner=row["visible_to_planner"],
            enabled=subs.get(name),  # None when not explicitly set
        ))

    return {
        "tools": [e.dict() for e in entries],
        "categories": category_order,
    }


@router.put("/user/tools/{tool_name}", response_model=dict)
def put_user_tool(
    tool_name: str,
    body: ToolSubscriptionRequest,
    user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Enable or disable a single tool for the authenticated user.

    The change takes effect on the NEXT chat turn (ctx.allowed_tools is
    resolved fresh each turn from the DB). No session restart needed.

    Returns the updated subscription row.
    """
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required for tool settings")

    # Validate tool name against the catalog (prevent stray writes)
    catalog = registry.skills_catalog()
    known = {row["name"] for row in catalog}
    if tool_name not in known:
        raise HTTPException(
            status_code=404,
            detail=f"Tool {tool_name!r} not found in catalog. "
                   f"Known tools: {sorted(known)[:10]}…",
        )

    try:
        from app.storage.tool_policy import set_user_tool_subscription
        set_user_tool_subscription(user_id, tool_name, body.enabled)
    except Exception as exc:
        logger.error("put_user_tool: write failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"tool_name": tool_name, "enabled": body.enabled, "status": "ok"}


@router.delete("/user/tools/{tool_name}", response_model=dict)
def delete_user_tool(
    tool_name: str,
    user_id: str | None = Depends(require_user),
) -> dict[str, Any]:
    """Reset one tool to the mode default (delete the explicit subscription row).

    After this call the tool's enabled state returns to None in GET /user/tools.
    """
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required for tool settings")

    try:
        from app.storage.tool_policy import delete_user_tool_subscription
        delete_user_tool_subscription(user_id, tool_name)
    except Exception as exc:
        logger.error("delete_user_tool: failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"tool_name": tool_name, "enabled": None, "status": "reset_to_default"}


@router.post("/user/tools/reset", response_model=dict)
def reset_user_tools(user_id: str | None = Depends(require_user)) -> dict[str, Any]:
    """Delete ALL tool subscription rows for the user (full reset to mode defaults).

    Useful for the "Restore defaults" button in the settings UI.
    """
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required for tool settings")

    try:
        from app.storage.tool_policy import reset_user_tool_subscriptions
        reset_user_tool_subscriptions(user_id)
    except Exception as exc:
        logger.error("reset_user_tools: failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"status": "reset", "message": "All tool subscriptions cleared. Mode defaults restored."}
