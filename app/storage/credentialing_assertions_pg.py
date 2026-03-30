"""Persist unified credentialing assertions (Postgres): org NPIs, locations, provider links."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

STEP_ASSERTIONS = frozenset({"identify_org", "find_locations", "find_associated_providers"})


def _db_url() -> str:
    from app.chat_config import get_chat_config

    return (get_chat_config().rag.database_url or "").strip()


def _canon_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def material_hash_for(material: dict[str, Any]) -> str:
    return hashlib.sha256(_canon_json(material).encode("utf-8")).hexdigest()[:48]


def _subject_key_org_npi(step_id: str, org_name: str, npi: str) -> str:
    base = f"{(org_name or '').strip().lower()}|{step_id}|org_npi|{npi}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:40]


def _subject_key_location(step_id: str, org_name: str, loc: dict[str, Any]) -> str:
    lid = str(loc.get("location_id") or "").strip()
    if lid:
        base = f"{(org_name or '').strip().lower()}|{step_id}|loc|{lid}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:40]
    addr = "|".join(
        str(loc.get(k) or "")
        for k in (
            "site_address_line_1",
            "site_address",
            "site_city",
            "site_state",
            "site_zip5",
            "site_zip",
        )
    )
    base = f"{(org_name or '').strip().lower()}|{step_id}|addr|{addr}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:40]


def _subject_key_provider(org_name: str, location_id: str, npi: str) -> str:
    n = str(npi).strip().zfill(10)
    base = f"{(org_name or '').strip().lower()}|provider_link|{location_id}|{n}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:40]


def _active_pairs(active_roster: Any) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    if not isinstance(active_roster, dict):
        return out
    for loc_id, rows in active_roster.items():
        for p in rows or []:
            if isinstance(p, dict) and p.get("npi"):
                out.add((str(loc_id), str(p["npi"]).strip().zfill(10)))
    return out


def facts_from_credentialing_state(
    step_id: str,
    org_name: str,
    *,
    org_npis: list[str],
    locations: list[Any],
    associated_providers: dict[str, list[Any]],
    active_roster: dict[str, list[Any]],
) -> list[dict[str, Any]]:
    """Build fact rows for persistence (material + dimensions)."""
    on = (org_name or "").strip()
    out: list[dict[str, Any]] = []
    if step_id == "identify_org":
        for raw in org_npis or []:
            npi = str(raw).strip().zfill(10)
            if len(npi) != 10:
                continue
            material = {"npi": npi}
            out.append(
                {
                    "fact_kind": "org_npi",
                    "subject_stable_key": _subject_key_org_npi(step_id, on, npi),
                    "org_npi": npi,
                    "location_id": None,
                    "provider_npi": npi,
                    "location_address_snapshot": None,
                    "provider_name_snapshot": None,
                    "association_strength": None,
                    "rationales_json": [],
                    "payload_json": material,
                    "material_hash": material_hash_for(material),
                }
            )
        return out
    if step_id == "find_locations":
        for loc in locations or []:
            if not isinstance(loc, dict):
                continue
            lid = str(loc.get("location_id") or "").strip() or None
            addr = (loc.get("site_address_line_1") or loc.get("site_address") or "") or ""
            material = {
                "location_id": lid,
                "npi": str(loc.get("npi") or loc.get("org_npi") or "").strip(),
                "site_address_line_1": (loc.get("site_address_line_1") or loc.get("site_address") or ""),
                "site_city": loc.get("site_city"),
                "site_state": loc.get("site_state"),
                "site_zip5": loc.get("site_zip5") or loc.get("site_zip"),
                "site_source": loc.get("site_source"),
            }
            sk = _subject_key_location(step_id, on, loc)
            out.append(
                {
                    "fact_kind": "location",
                    "subject_stable_key": sk,
                    "org_npi": str(loc.get("npi") or loc.get("org_npi") or "").strip() or None,
                    "location_id": lid,
                    "provider_npi": None,
                    "location_address_snapshot": str(addr)[:500] or None,
                    "provider_name_snapshot": None,
                    "association_strength": None,
                    "rationales_json": [],
                    "payload_json": material,
                    "material_hash": material_hash_for(material),
                }
            )
        return out
    if step_id == "find_associated_providers":
        active = _active_pairs(active_roster)
        if not isinstance(associated_providers, dict):
            return out
        for loc_id, rows in associated_providers.items():
            lid = str(loc_id)
            for p in rows or []:
                if not isinstance(p, dict):
                    continue
                npi = str(p.get("npi") or "").strip().zfill(10)
                if len(npi) != 10:
                    continue
                reasons = p.get("inclusion_reasons") or []
                if isinstance(reasons, str):
                    rat = [reasons] if reasons else []
                else:
                    rat = [str(x) for x in reasons if x]
                score = p.get("association_likelihood")
                try:
                    score_i = int(score) if score is not None else None
                except (TypeError, ValueError):
                    score_i = None
                material = {
                    "location_id": lid,
                    "npi": npi,
                    "name": (p.get("name") or p.get("provider_name") or "") or "",
                    "association_likelihood": score_i,
                    "roster_status": p.get("roster_status"),
                    "in_active_roster": (lid, npi) in active,
                    "match_type": p.get("match_type"),
                }
                sk = _subject_key_provider(on, lid, npi)
                out.append(
                    {
                        "fact_kind": "provider_link",
                        "subject_stable_key": sk,
                        "org_npi": None,
                        "location_id": lid,
                        "provider_npi": npi,
                        "location_address_snapshot": None,
                        "provider_name_snapshot": str(material["name"])[:300] if material.get("name") else None,
                        "association_strength": score_i,
                        "rationales_json": rat,
                        "payload_json": material,
                        "material_hash": material_hash_for(material),
                    }
                )
        return out
    return out


def assertion_sync_summary_line(
    *,
    step_id: str,
    mode: str,
    added: int,
    deleted: int,
    validated: int,
    revised: int,
) -> str:
    """Single user-facing line for chat / progress (◌ prefix matches other credentialing emits)."""
    parts: list[str] = []
    if added:
        parts.append(f"{added} added")
    if validated:
        parts.append(f"{validated} validated")
    if revised:
        parts.append(f"{revised} revised (new version)")
    if deleted:
        parts.append(f"{deleted} removed (closed)")
    body = ", ".join(parts) if parts else "no assertion row changes"
    return f"◌ credentialing_assertion ({step_id}, {mode}): {body}."


def _sync_facts(
    conn: Any,
    cur: Any,
    *,
    credentialing_run_id: str,
    thread_id: str | None,
    org_name: str,
    step_id: str,
    mode: str,
    policy_version: str | None,
    ruleset_hash: str | None,
    facts: list[dict[str, Any]],
    status_initial_insert: str,
    status_material_revision: str,
    status_determined_by_touch: str,
) -> dict[str, int]:
    delta = {"added": 0, "deleted": 0, "validated": 0, "revised": 0}
    run_id = (credentialing_run_id or "").strip()
    if not run_id:
        return delta
    mode_s = mode if mode in ("copilot", "autopilot") else "copilot"
    keys_in_payload = {f["subject_stable_key"] for f in facts}

    cur.execute(
        """
        SELECT id, subject_stable_key FROM credentialing_assertion
        WHERE credentialing_run_id = %s AND step_id = %s AND valid_to IS NULL
        """,
        (run_id, step_id),
    )
    existing_open = {row[1]: row[0] for row in cur.fetchall()}
    for sk, row_id in existing_open.items():
        if sk not in keys_in_payload:
            cur.execute(
                """
                UPDATE credentialing_assertion SET valid_to = now(), updated_at = now()
                WHERE id = %s
                """,
                (row_id,),
            )
            delta["deleted"] += 1

    for fact in facts:
        sk = fact["subject_stable_key"]
        cur.execute(
            """
            SELECT id, assertion_group_id, material_hash
            FROM credentialing_assertion
            WHERE credentialing_run_id = %s AND subject_stable_key = %s AND valid_to IS NULL
            FOR UPDATE
            """,
            (run_id, sk),
        )
        row = cur.fetchone()
        if row is None:
            gid = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO credentialing_assertion (
                  id, assertion_group_id, credentialing_run_id, thread_id, org_name, step_id,
                  fact_kind, subject_stable_key, org_npi, location_id, provider_npi,
                  location_address_snapshot, provider_name_snapshot, association_strength,
                  rationales_json, payload_json, material_hash, mode, status, status_determined_by,
                  policy_version, ruleset_hash, valid_from, valid_to, validated_at, created_at, updated_at
                ) VALUES (
                  %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s,
                  %s, %s, %s,
                  %s::jsonb, %s::jsonb, %s, %s, 'active', %s,
                  %s, %s, now(), NULL,
                  CASE WHEN %s = 'autopilot' THEN NULL ELSE now() END,
                  now(), now()
                )
                """,
                (
                    str(uuid.uuid4()),
                    gid,
                    run_id,
                    thread_id or None,
                    org_name or "",
                    step_id,
                    fact["fact_kind"],
                    sk,
                    fact.get("org_npi"),
                    fact.get("location_id"),
                    fact.get("provider_npi"),
                    fact.get("location_address_snapshot"),
                    fact.get("provider_name_snapshot"),
                    fact.get("association_strength"),
                    json.dumps(fact.get("rationales_json") or []),
                    json.dumps(fact.get("payload_json") or {}),
                    fact["material_hash"],
                    mode_s,
                    status_initial_insert,
                    policy_version,
                    ruleset_hash,
                    mode_s,
                ),
            )
            delta["added"] += 1
            continue
        _id, group_id, prev_hash = row[0], str(row[1]), row[2]
        if (prev_hash or "") == fact["material_hash"]:
            cur.execute(
                """
                UPDATE credentialing_assertion
                SET validated_at = now(), updated_at = now(),
                    status_determined_by = %s,
                    policy_version = COALESCE(%s, policy_version),
                    ruleset_hash = COALESCE(%s, ruleset_hash),
                    thread_id = COALESCE(%s, thread_id)
                WHERE id = %s
                """,
                (
                    status_determined_by_touch,
                    policy_version,
                    ruleset_hash,
                    thread_id or None,
                    _id,
                ),
            )
            delta["validated"] += 1
        else:
            cur.execute(
                """
                UPDATE credentialing_assertion SET valid_to = now(), updated_at = now() WHERE id = %s
                """,
                (_id,),
            )
            cur.execute(
                """
                INSERT INTO credentialing_assertion (
                  id, assertion_group_id, credentialing_run_id, thread_id, org_name, step_id,
                  fact_kind, subject_stable_key, org_npi, location_id, provider_npi,
                  location_address_snapshot, provider_name_snapshot, association_strength,
                  rationales_json, payload_json, material_hash, mode, status, status_determined_by,
                  policy_version, ruleset_hash, valid_from, valid_to, validated_at, created_at, updated_at
                ) VALUES (
                  %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s,
                  %s, %s, %s,
                  %s::jsonb, %s::jsonb, %s, %s, 'active', %s,
                  %s, %s, now(), NULL, now(), now(), now()
                )
                """,
                (
                    str(uuid.uuid4()),
                    group_id,
                    run_id,
                    thread_id or None,
                    org_name or "",
                    step_id,
                    fact["fact_kind"],
                    sk,
                    fact.get("org_npi"),
                    fact.get("location_id"),
                    fact.get("provider_npi"),
                    fact.get("location_address_snapshot"),
                    fact.get("provider_name_snapshot"),
                    fact.get("association_strength"),
                    json.dumps(fact.get("rationales_json") or []),
                    json.dumps(fact.get("payload_json") or {}),
                    fact["material_hash"],
                    mode_s,
                    status_material_revision,
                    policy_version,
                    ruleset_hash,
                ),
            )
            delta["revised"] += 1
    return delta


def persist_assertions_after_validate(
    credentialing_run_id: str,
    thread_id: str | None,
    org_name: str,
    step_id: str,
    mode: str,
    *,
    org_npis: list[str],
    locations: list[Any],
    associated_providers: dict[str, list[Any]],
    active_roster: dict[str, list[Any]],
    policy_version: str | None = None,
    ruleset_hash: str | None = None,
) -> dict[str, Any] | None:
    if step_id not in STEP_ASSERTIONS:
        return None
    url = _db_url()
    if not url:
        return {
            "persisted": False,
            "reason": "no_database_url",
            "table": "credentialing_assertion",
            "step_id": step_id,
        }
    facts = facts_from_credentialing_state(
        step_id,
        org_name,
        org_npis=org_npis,
        locations=locations,
        associated_providers=associated_providers,
        active_roster=active_roster,
    )
    try:
        import psycopg2

        conn = psycopg2.connect(url)
        cur = conn.cursor()
        try:
            delta = _sync_facts(
                conn,
                cur,
                credentialing_run_id=credentialing_run_id,
                thread_id=thread_id,
                org_name=org_name,
                step_id=step_id,
                mode=mode,
                policy_version=policy_version,
                ruleset_hash=ruleset_hash,
                facts=facts,
                status_initial_insert="user_validation",
                status_material_revision="user_edit",
                status_determined_by_touch="user_validation",
            )
            conn.commit()
        finally:
            cur.close()
            conn.close()
        line = assertion_sync_summary_line(
            step_id=step_id,
            mode=mode,
            added=delta["added"],
            deleted=delta["deleted"],
            validated=delta["validated"],
            revised=delta["revised"],
        )
        return {
            "persisted": True,
            "table": "credentialing_assertion",
            "step_id": step_id,
            "mode": mode,
            "counts": dict(delta),
            "emit": line,
        }
    except Exception as e:
        logger.warning("persist_assertions_after_validate failed: %s", e)
        return {"persisted": False, "error": str(e), "table": "credentialing_assertion", "step_id": step_id}


def persist_autopilot_snapshot(
    credentialing_run_id: str,
    thread_id: str | None,
    org_name: str,
    *,
    org_npis: list[str],
    locations: list[Any],
    associated_providers: dict[str, list[Any]],
    active_roster: dict[str, list[Any]],
    policy_version: str | None = None,
    ruleset_hash: str | None = None,
) -> dict[str, Any] | None:
    """Insert open assertions for early credentialing steps after a full autopilot run."""
    url = _db_url()
    if not url:
        return {"persisted": False, "reason": "no_database_url", "table": "credentialing_assertion"}
    try:
        import psycopg2

        conn = psycopg2.connect(url)
        cur = conn.cursor()
        steps_out: list[dict[str, Any]] = []
        totals = {"added": 0, "deleted": 0, "validated": 0, "revised": 0}
        emit_lines: list[str] = []
        try:
            for sid in ("identify_org", "find_locations", "find_associated_providers"):
                facts = facts_from_credentialing_state(
                    sid,
                    org_name,
                    org_npis=org_npis,
                    locations=locations,
                    associated_providers=associated_providers,
                    active_roster=active_roster,
                )
                delta = _sync_facts(
                    conn,
                    cur,
                    credentialing_run_id=credentialing_run_id,
                    thread_id=thread_id,
                    org_name=org_name,
                    step_id=sid,
                    mode="autopilot",
                    policy_version=policy_version,
                    ruleset_hash=ruleset_hash,
                    facts=facts,
                    status_initial_insert="autopilot_policy",
                    status_material_revision="autopilot_policy",
                    status_determined_by_touch="autopilot_policy",
                )
                for k in totals:
                    totals[k] += delta[k]
                line = assertion_sync_summary_line(
                    step_id=sid,
                    mode="autopilot",
                    added=delta["added"],
                    deleted=delta["deleted"],
                    validated=delta["validated"],
                    revised=delta["revised"],
                )
                emit_lines.append(line)
                steps_out.append({"step_id": sid, "counts": dict(delta), "emit": line})
            conn.commit()
        finally:
            cur.close()
            conn.close()
        return {
            "persisted": True,
            "table": "credentialing_assertion",
            "mode": "autopilot",
            "steps": steps_out,
            "totals": totals,
            "emit": emit_lines,
        }
    except Exception as e:
        logger.warning("persist_autopilot_snapshot failed: %s", e)
        return {"persisted": False, "error": str(e), "table": "credentialing_assertion"}
