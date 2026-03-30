"""Persist roster review sessions and line items (Postgres)."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


def _db_url() -> str:
    from app.chat_config import get_chat_config
    return (get_chat_config().rag.database_url or "").strip()


def stable_row_key(location_id: str, npi: str) -> str:
    raw = f"{location_id}|{npi}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def persist_roster_review_from_validate(
    credentialing_run_id: str,
    thread_id: str | None,
    org_name: str,
    org_npis: list[str],
    step_id: str,
    *,
    mode: str,
    validated_output: dict[str, Any],
    policy_version: str | None = None,
    ruleset_hash: str | None = None,
) -> str | None:
    """
    Insert confirmed roster_review_session + roster_line_items from validate payload.
    Returns session id UUID string or None if DB unavailable.
    """
    url = _db_url()
    if not url:
        return None
    line_items_in = validated_output.get("roster_line_items")
    if not isinstance(line_items_in, list):
        line_items_in = _synthesize_line_items(
            validated_output.get("associated_providers"),
            validated_output.get("active_roster"),
        )
    try:
        import psycopg2
        sid = str(uuid.uuid4())
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO roster_review_session (
                id, credentialing_run_id, thread_id, org_name, org_npis_json,
                step_id, mode, policy_version, ruleset_hash, status, confirmed_at, metadata_json
            ) VALUES (
                %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, 'confirmed', now(), %s::jsonb
            )
            """,
            (
                sid,
                credentialing_run_id or None,
                thread_id or None,
                org_name or "",
                json.dumps([str(x) for x in (org_npis or [])]),
                step_id,
                mode if mode in ("copilot", "autopilot") else "copilot",
                policy_version,
                ruleset_hash,
                json.dumps(
                {k: v for k, v in validated_output.items() if k != "roster_line_items"},
                default=str,
            ),
            ),
        )
        for i, row in enumerate(line_items_in):
            if not isinstance(row, dict):
                continue
            lid = str(row.get("location_id") or "")
            npi = str(row.get("npi") or "").strip().zfill(10)
            rk = str(row.get("stable_row_key") or "") or stable_row_key(lid, npi)
            cur.execute(
                """
                INSERT INTO roster_line_item (
                    session_id, stable_row_key, location_id, location_address_snapshot,
                    npi, name_snapshot, model_score, model_rationale,
                    user_verdict, user_note, edited_fields_json, source, sort_order
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    sid,
                    rk,
                    lid,
                    row.get("location_address_snapshot"),
                    npi,
                    row.get("name_snapshot"),
                    row.get("model_score"),
                    row.get("model_rationale"),
                    row.get("user_verdict"),
                    row.get("user_note"),
                    json.dumps(row.get("edited_fields") or {}),
                    row.get("source") if row.get("source") in ("model", "user_added") else "model",
                    i,
                ),
            )
        conn.commit()
        cur.close()
        conn.close()
        return sid
    except Exception as e:
        logger.warning("persist_roster_review_from_validate failed: %s", e)
        return None


def _synthesize_line_items(
    associated: Any,
    active: Any,
) -> list[dict[str, Any]]:
    active_set: set[tuple[str, str]] = set()
    if isinstance(active, dict):
        for loc_id, rows in active.items():
            for p in rows or []:
                if isinstance(p, dict) and p.get("npi"):
                    active_set.add((str(loc_id), str(p["npi"]).strip().zfill(10)))
    out: list[dict[str, Any]] = []
    if not isinstance(associated, dict):
        return out
    for loc_id, rows in associated.items():
        for p in rows or []:
            if not isinstance(p, dict):
                continue
            npi = str(p.get("npi") or "").strip().zfill(10)
            if len(npi) != 10:
                continue
            in_active = (str(loc_id), npi) in active_set
            out.append(
                {
                    "location_id": str(loc_id),
                    "npi": npi,
                    "name_snapshot": p.get("name"),
                    "model_score": p.get("association_likelihood"),
                    "model_rationale": p.get("roster_rationale"),
                    "user_verdict": "accept" if in_active else None,
                    "source": "model",
                }
            )
    return out
