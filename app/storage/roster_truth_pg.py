"""
Roster truth table — validated provider roster per org.

`roster_truth`  : one row per (org, provider_key); tracks the validated NPI,
                  name, specialty. Drives the delta-diff on every new run.

`roster_snooze` : acknowledged mismatches with a value-fingerprint. Suppressed
                  on subsequent runs until either roster_val or nppes_val changes.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

logger = logging.getLogger(__name__)


def _db_url() -> str:
    from app.chat_config import get_chat_config
    return (get_chat_config().rag.database_url or "").strip()


# ── Schema bootstrap ───────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS roster_truth (
    id              SERIAL PRIMARY KEY,
    org_name        TEXT    NOT NULL,
    provider_key    TEXT    NOT NULL,           -- NPI if known, else normalised name
    provider_name   TEXT,
    npi_roster      TEXT,                        -- as supplied in the roster file
    npi_validated   TEXT,                        -- NPPES-confirmed NPI
    specialty       TEXT,
    match_confidence FLOAT,
    decision        TEXT    DEFAULT 'validated', -- 'validated' | 'rejected' | 'excluded'
    run_id          TEXT,
    validated_at    TIMESTAMPTZ DEFAULT NOW(),
    invalidated_at  TIMESTAMPTZ,                 -- NULL = still active
    UNIQUE (org_name, provider_key)
);

CREATE TABLE IF NOT EXISTS roster_snooze (
    id              SERIAL PRIMARY KEY,
    org_name        TEXT    NOT NULL,
    provider_key    TEXT    NOT NULL,
    dimension       TEXT    NOT NULL,           -- 'name'|'taxonomy'|'address'|'status'
    roster_val      TEXT,
    nppes_val       TEXT,
    snoozed_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,                -- NULL = indefinite
    UNIQUE (org_name, provider_key, dimension)
);

CREATE INDEX IF NOT EXISTS idx_rt_org  ON roster_truth(org_name);
CREATE INDEX IF NOT EXISTS idx_rs_org  ON roster_snooze(org_name);
"""


def ensure_schema() -> None:
    url = _db_url()
    if not url:
        return
    try:
        import psycopg2
        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(_DDL)
            conn.commit()
    except Exception as e:
        logger.warning("roster_truth ensure_schema failed: %s", e)


# ── Normalisation helpers ──────────────────────────────────────────────────────

def _norm_name(name: str) -> str:
    """Lower, strip accents, collapse whitespace, remove punctuation."""
    s = (name or "").strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9 ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _provider_key(npi: str | None, name: str) -> str:
    return npi.strip() if npi and npi.strip() else _norm_name(name)


# ── roster_truth CRUD ──────────────────────────────────────────────────────────

def get_truth_for_org(org_name: str) -> list[dict[str, Any]]:
    """Return all active validated providers for an org."""
    url = _db_url()
    if not url:
        return []
    try:
        import psycopg2
        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT provider_key, provider_name, npi_roster, npi_validated,
                           specialty, match_confidence, decision, run_id, validated_at
                    FROM roster_truth
                    WHERE lower(org_name) = lower(%s)
                      AND invalidated_at IS NULL
                    ORDER BY provider_name
                    """,
                    (org_name,),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.warning("get_truth_for_org failed: %s", e)
        return []


def upsert_providers(org_name: str, providers: list[dict[str, Any]], run_id: str | None = None) -> int:
    """
    Insert or update validated providers from a run.
    Each provider dict needs at least: provider_name, npi_validated (or npi_roster), decision.
    Returns count of upserted rows.
    """
    url = _db_url()
    if not url or not providers:
        return 0
    ensure_schema()
    count = 0
    try:
        import psycopg2
        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                for p in providers:
                    name    = (p.get("provider_name") or "").strip()
                    npi_v   = (p.get("npi_validated") or p.get("npi_roster") or "").strip() or None
                    npi_r   = (p.get("npi_roster") or "").strip() or None
                    key     = _provider_key(npi_v or npi_r, name)
                    if not key:
                        continue
                    cur.execute(
                        """
                        INSERT INTO roster_truth
                            (org_name, provider_key, provider_name, npi_roster,
                             npi_validated, specialty, match_confidence, decision, run_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (org_name, provider_key) DO UPDATE SET
                            provider_name    = EXCLUDED.provider_name,
                            npi_roster       = EXCLUDED.npi_roster,
                            npi_validated    = EXCLUDED.npi_validated,
                            specialty        = EXCLUDED.specialty,
                            match_confidence = EXCLUDED.match_confidence,
                            decision         = EXCLUDED.decision,
                            run_id           = EXCLUDED.run_id,
                            validated_at     = NOW(),
                            invalidated_at   = NULL
                        """,
                        (
                            org_name, key, name or None, npi_r, npi_v,
                            p.get("specialty") or None,
                            p.get("match_confidence"),
                            p.get("decision") or "validated",
                            run_id,
                        ),
                    )
                    count += cur.rowcount
            conn.commit()
    except Exception as e:
        logger.warning("upsert_providers failed: %s", e)
    return count


# ── Diff engine ────────────────────────────────────────────────────────────────

def diff_roster_against_truth(
    org_name: str,
    new_providers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Compare a freshly uploaded provider list against the validated truth table.

    Returns the same list with two extra keys added to each row:
      change_type : 'unchanged' | 'new' | 'changed' | 'removed'
      truth_match : the matching truth row (or None)
      field_changes: list of changed field dicts  {field, roster_val, truth_val}
    Plus appended rows for 'removed' providers (in truth but not in new upload).
    """
    truth = get_truth_for_org(org_name)

    # Index truth by NPI and by normalised name
    truth_by_npi  = {t["npi_validated"]: t for t in truth if t["npi_validated"]}
    truth_by_npi.update({t["npi_roster"]: t for t in truth if t["npi_roster"] and t["npi_roster"] not in truth_by_npi})
    truth_by_name = {_norm_name(t["provider_name"] or ""): t for t in truth if t["provider_name"]}

    seen_keys: set[str] = set()
    result: list[dict[str, Any]] = []

    for p in new_providers:
        name  = (p.get("provider_name") or "").strip()
        npi   = (p.get("npi_validated") or p.get("npi_roster") or "").strip() or None

        # Match: NPI first, normalised name second
        match = None
        if npi:
            match = truth_by_npi.get(npi)
        if not match:
            match = truth_by_name.get(_norm_name(name))

        if match:
            seen_keys.add(match["provider_key"])
            changes = _detect_changes(p, match)
            row = {
                **p,
                "change_type":   "changed" if changes else "unchanged",
                "truth_match":   match,
                "field_changes": changes,
            }
        else:
            row = {**p, "change_type": "new", "truth_match": None, "field_changes": []}

        result.append(row)

    # Append removed providers (in truth but absent from new upload)
    for t in truth:
        if t["provider_key"] not in seen_keys and t["decision"] != "excluded":
            result.append({
                "provider_name":  t["provider_name"],
                "npi_roster":     t["npi_roster"],
                "npi_validated":  t["npi_validated"],
                "specialty":      t["specialty"],
                "change_type":    "removed",
                "truth_match":    t,
                "field_changes":  [],
            })

    return result


def _detect_changes(new: dict, truth: dict) -> list[dict[str, str]]:
    changes = []
    # Name drift
    new_name   = _norm_name(new.get("provider_name") or "")
    truth_name = _norm_name(truth.get("provider_name") or "")
    if new_name and truth_name and new_name != truth_name:
        changes.append({"field": "name", "roster_val": new.get("provider_name") or "", "truth_val": truth.get("provider_name") or ""})
    # Specialty change
    new_spec   = (new.get("specialty") or "").strip().lower()
    truth_spec = (truth.get("specialty") or "").strip().lower()
    if new_spec and truth_spec and new_spec != truth_spec:
        changes.append({"field": "specialty", "roster_val": new.get("specialty") or "", "truth_val": truth.get("specialty") or ""})
    return changes


# ── roster_snooze CRUD ────────────────────────────────────────────────────────

def snooze_mismatch(
    org_name: str,
    provider_key: str,
    dimension: str,
    roster_val: str,
    nppes_val: str,
    expires_at: str | None = None,
) -> bool:
    url = _db_url()
    if not url:
        return False
    ensure_schema()
    try:
        import psycopg2
        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO roster_snooze
                        (org_name, provider_key, dimension, roster_val, nppes_val, expires_at)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (org_name, provider_key, dimension) DO UPDATE SET
                        roster_val = EXCLUDED.roster_val,
                        nppes_val  = EXCLUDED.nppes_val,
                        snoozed_at = NOW(),
                        expires_at = EXCLUDED.expires_at
                    """,
                    (org_name, provider_key, dimension, roster_val, nppes_val, expires_at),
                )
            conn.commit()
        return True
    except Exception as e:
        logger.warning("snooze_mismatch failed: %s", e)
        return False


def get_snoozes_for_org(org_name: str) -> list[dict[str, Any]]:
    """Return all active (non-expired) snoozes for an org."""
    url = _db_url()
    if not url:
        return []
    ensure_schema()
    try:
        import psycopg2
        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT provider_key, dimension, roster_val, nppes_val, snoozed_at, expires_at
                    FROM roster_snooze
                    WHERE lower(org_name) = lower(%s)
                      AND (expires_at IS NULL OR expires_at > NOW())
                    """,
                    (org_name,),
                )
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.warning("get_snoozes_for_org failed: %s", e)
        return []


def wake_up_stale_snoozes(
    org_name: str,
    current_provider_states: list[dict[str, Any]],
) -> list[str]:
    """
    Remove snoozes whose fingerprint (roster_val / nppes_val) no longer matches
    the current state. Returns list of woken-up provider_key values.
    """
    snoozes = get_snoozes_for_org(org_name)
    if not snoozes:
        return []

    # Build a lookup: (provider_key, dimension) → (roster_val, nppes_val)
    current_lookup: dict[tuple, tuple] = {}
    for p in current_provider_states:
        key = _provider_key(p.get("npi_validated") or p.get("npi_roster"), p.get("provider_name") or "")
        align = p.get("alignment") or {}
        for dim in ("name", "taxonomy", "address", "status"):
            dim_data = align.get(dim) or {}
            current_lookup[(key, dim)] = (
                str(dim_data.get("roster") or ""),
                str(dim_data.get("nppes")  or ""),
            )

    url = _db_url()
    woken: list[str] = []
    if not url:
        return woken
    try:
        import psycopg2
        with psycopg2.connect(url) as conn:
            with conn.cursor() as cur:
                for s in snoozes:
                    pkey = s["provider_key"]
                    dim  = s["dimension"]
                    cur_vals = current_lookup.get((pkey, dim))
                    if cur_vals is None:
                        continue
                    cur_roster, cur_nppes = cur_vals
                    if cur_roster != (s["roster_val"] or "") or cur_nppes != (s["nppes_val"] or ""):
                        # Fingerprint broke — wake it up
                        cur.execute(
                            "DELETE FROM roster_snooze WHERE org_name=%s AND provider_key=%s AND dimension=%s",
                            (org_name, pkey, dim),
                        )
                        woken.append(pkey)
            conn.commit()
    except Exception as e:
        logger.warning("wake_up_stale_snoozes failed: %s", e)
    return woken
