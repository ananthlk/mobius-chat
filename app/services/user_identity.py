"""Thin client for the mobius-user identity service.

Two reads, both used by the task skills:

- ``resolve_self(subject)`` — map chat's authenticated identity (the
  ``require_user`` user_id, which mobius-user confirmed IS the canonical
  ``app_user.user_id``) to an ``assignee_ref`` like ``user:<uuid>``.
  Powers "tasks assigned to me".
- ``resolve_assignee(query, org)`` — natural-language → ranked candidate
  users/agents ("assign this to Sam"). ``org`` is a ranking boost on the
  service side, never a hard filter.

Contract (agreed with the User Manager Agent, 2026-07-08):
- 404 from by-identity = unknown subject → return None; callers fall
  back to UNSCOPED behavior, never guess.
- ``assignee_ref`` comes pre-formatted from the service (``user:{id}`` /
  ``agent:{name}``) — never format refs locally.
- Auth: ``X-Internal-Key`` from the MOBIUS_USER_INTERNAL_KEY env
  (Secret Manager, mounted by deploy.sh).

All calls are best-effort with a short timeout and a small in-process
TTL cache — identity resolution must never add meaningful latency to a
turn or break task listing when mobius-user is down.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_TIMEOUT_S = 3.0
_CACHE_TTL_S = 300.0
_cache: dict[str, tuple[float, Any]] = {}


def _base() -> str:
    return (os.environ.get("MOBIUS_USER_URL") or "").rstrip("/")


def _key() -> str:
    return (os.environ.get("MOBIUS_USER_INTERNAL_KEY") or "").strip()


def _get(path: str, params: dict[str, str]) -> dict[str, Any] | None:
    """GET with internal-key auth. None on any failure (incl. 404)."""
    base = _base()
    if not base or not _key():
        return None
    url = f"{base}/api/v1/users{path}?{urllib.parse.urlencode(params)}"
    cached = _cache.get(url)
    now = time.monotonic()
    if cached and now - cached[0] < _CACHE_TTL_S:
        return cached[1]
    try:
        req = urllib.request.Request(url, headers={"X-Internal-Key": _key()})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
        _cache[url] = (now, data)
        return data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _cache[url] = (now, None)  # negative-cache unknown subjects too
        else:
            logger.debug("user_identity GET %s failed: %s", path, e)
        return None
    except Exception as e:
        logger.debug("user_identity GET %s failed: %s", path, e)
        return None


def resolve_self(subject: str | None) -> dict[str, Any] | None:
    """Chat identity → {user_id, display_name, assignee_ref, …} or None."""
    if not subject or not str(subject).strip():
        return None
    data = _get("/by-identity", {"subject": str(subject).strip()})
    user = (data or {}).get("user")
    return user if isinstance(user, dict) and user.get("assignee_ref") else None


def resolve_assignee(query: str, org: str | None = None) -> dict[str, Any] | None:
    """Best candidate for a natural-language assignee, or None.

    Returns the top-ranked candidate only when the service returns at
    least one; ambiguity handling (asking "which Sam?") is the caller's
    job when it wants it — v1 takes the top rank.
    """
    q = (query or "").strip()
    if not q:
        return None
    params: dict[str, str] = {"q": q, "limit": "3"}
    if org:
        params["org"] = org
    data = _get("/resolve", params)
    cands = (data or {}).get("candidates") or []
    top = cands[0] if cands and isinstance(cands[0], dict) else None
    return top if top and top.get("assignee_ref") else None


def directory_search(
    org_slug: str,
    q: str | None = None,
    limit: int = 20,
    exclude_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Org-scoped coworker lookup for @-mention autocomplete.

    org_slug is a HARD filter — results never cross org boundaries.
    Returns [{user_id, display_name, email, assignee_ref, is_agent, roles}].
    Excludes the calling user when exclude_user_id is provided.
    """
    params: dict[str, str] = {"org_slug": org_slug, "limit": str(limit)}
    if q and q.strip():
        params["q"] = q.strip()
    # Directory lives under /api/v1/users/directory — bypass _get's /users prefix
    base = _base()
    key = _key()
    if not base or not key:
        return []
    url = f"{base}/api/v1/users/directory?{urllib.parse.urlencode(params)}"
    cached = _cache.get(url)
    now = time.monotonic()
    _DIRECTORY_TTL = 120  # 2 min — roster changes are rare
    if cached and now - cached[0] < _DIRECTORY_TTL:
        members = cached[1]
    else:
        try:
            req = urllib.request.Request(url, headers={"X-Internal-Key": key})
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
            members = (data or {}).get("members") or []
            _cache[url] = (now, members)
        except Exception as e:
            logger.debug("directory_search failed: %s", e)
            return []
    if exclude_user_id:
        members = [m for m in members if m.get("user_id") != exclude_user_id]
    return members
