"""Resolve roster upload_id from provider-roster-credentialing (Postgres + GCS), not chat thread memory."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


def roster_skill_base_url() -> str:
    return (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").strip().rstrip("/").split("/report")[0]


def normalize_billing_npi(org_id: str | None) -> str:
    d = re.sub(r"\D", "", str(org_id or ""))
    if len(d) >= 10:
        return d[:10].zfill(10)
    return d.zfill(10) if d else ""


def fetch_latest_resolved_upload_for_org(org_id: str | None) -> dict[str, Any] | None:
    """GET /roster-uploads/latest-resolved-for-org on the provider skill."""
    oid = normalize_billing_npi(org_id)
    if len(oid) != 10:
        return None
    base = roster_skill_base_url()
    if not base:
        return None
    try:
        import httpx

        url = f"{base}/roster-uploads/latest-resolved-for-org"
        with httpx.Client(timeout=20.0) as client:
            r = client.get(url, params={"org_id": oid})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("fetch_latest_resolved_upload_for_org failed: %s", e)
        return None


def resolve_reconciliation_upload_id_for_org(
    org_id: str | None,
    *,
    explicit_upload_id: str | None = None,
) -> str | None:
    """
    Reconciliation internal roster CSV:
    - If ``explicit_upload_id`` is set (tool override), use it.
    - Else use latest **resolved** row in skill DB for ``org_id``.
    """
    if (explicit_upload_id or "").strip():
        return (explicit_upload_id or "").strip()
    meta = fetch_latest_resolved_upload_for_org(org_id)
    if not meta:
        return None
    return (meta.get("upload_id") or "").strip() or None
