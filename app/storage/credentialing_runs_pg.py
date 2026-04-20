"""Postgres persistence for credentialing co-pilot runs (shared across API + worker processes)."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _db_url() -> str:
    """Resolve a psycopg2-ready DSN.

    Routes through ``app.db_client._get_fallback_url`` so two things
    the raw chat-config URL doesn't carry come along:

      * The ``postgresql+psycopg2://`` driver prefix gets stripped
        (psycopg2 rejects SQLAlchemy-style schemes).
      * ``CHAT_DB_PASSWORD`` (from Secret Manager in Cloud Run) gets
        injected at the user-segment so we don't need a password in
        the env URL.

    Previously this returned the raw ``chat_config.rag.database_url``
    which worked in dev (``.env`` already had a standard URL) but
    blew up in Cloud Run with ``invalid DSN`` and ``no password``
    errors.
    """
    from app.db_client import _get_fallback_url
    return _get_fallback_url("chat")


def save_credentialing_run_record(run_id: str, body: dict[str, Any]) -> bool:
    url = _db_url()
    if not url or not run_id:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO credentialing_runs (run_id, body, updated_at)
            VALUES (%s, %s::jsonb, now())
            ON CONFLICT (run_id) DO UPDATE SET
                body = EXCLUDED.body,
                updated_at = now()
            """,
            (run_id, json.dumps(body)),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.warning("save_credentialing_run_record failed: %s", e)
        return False


def load_credentialing_run_record(run_id: str) -> dict[str, Any] | None:
    url = _db_url()
    if not url or not run_id:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute("SELECT body FROM credentialing_runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row or row[0] is None:
            return None
        raw = row[0]
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str):
            return json.loads(raw)
        return json.loads(json.dumps(raw))
    except Exception as e:
        logger.warning("load_credentialing_run_record failed: %s", e)
        return None


def patch_step3_upload_id(run_id: str, reconciliation_upload_id: str) -> bool:
    """Atomically update step3_roster_upload_id in an existing run's orchestrator_state_dict.

    Called after a roster upload so the pipeline page can auto-load the last roster
    without the user having to re-upload every time.
    """
    url = _db_url()
    if not url or not run_id or not reconciliation_upload_id:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE credentialing_runs
            SET body = jsonb_set(
                body,
                '{orchestrator_state_dict,step3_roster_upload_id}',
                %s::jsonb
            ),
            updated_at = now()
            WHERE run_id = %s
            """,
            (json.dumps(reconciliation_upload_id), run_id),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.warning("patch_step3_upload_id failed: %s", e)
        return False


def patch_pml_task_state(run_id: str, task_state: dict) -> bool:
    """Persist PML task overrides (done set, notes, manual tasks) into the run's orchestrator_state.

    Stored as body->'orchestrator_state_dict'->'pml_task_state' so it is returned
    alongside the rest of orchestrator_state when the run is fetched.
    task_state shape: { done: [task_id, ...], notes: {task_id: note}, manual: [{...}] }
    """
    url = _db_url()
    if not url or not run_id:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE credentialing_runs
            SET body = jsonb_set(
                COALESCE(body, '{}'::jsonb),
                '{orchestrator_state_dict,pml_task_state}',
                %s::jsonb,
                true
            ),
            updated_at = now()
            WHERE run_id = %s
            """,
            (json.dumps(task_state), run_id),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.warning("patch_pml_task_state failed: %s", e)
        return False


def patch_taxonomy_task_state(run_id: str, task_state: dict) -> bool:
    """Persist taxonomy task overrides (done set, notes, dismissed) into the run's orchestrator_state.

    Stored as body->'orchestrator_state_dict'->'taxonomy_task_state' so it is returned
    alongside orchestrator_state when the run is fetched.
    task_state shape: { done: [task_id, ...], notes: {task_id: note}, dismissed: [task_id, ...] }
    """
    url = _db_url()
    if not url or not run_id:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE credentialing_runs
            SET body = jsonb_set(
                COALESCE(body, '{}'::jsonb),
                '{orchestrator_state_dict,taxonomy_task_state}',
                %s::jsonb,
                true
            ),
            updated_at = now()
            WHERE run_id = %s
            """,
            (json.dumps(task_state), run_id),
        )
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.warning("patch_taxonomy_task_state failed: %s", e)
        return False


def list_credentialing_runs(limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
    """Return lightweight run summaries (no full_state) ordered by most recently updated."""
    url = _db_url()
    if not url:
        return []
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                run_id,
                body->>'org_name'        AS org_name,
                body->>'mode'            AS mode,
                body->>'phase'           AS phase,
                body->>'pending_step_id' AS pending_step_id,
                body->'validated_outputs' AS validated_outputs,
                updated_at,
                created_at
            FROM credentialing_runs
            ORDER BY updated_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        results = []
        for row in rows:
            (run_id, org_name, mode, phase, pending_step_id,
             validated_outputs_raw, updated_at, created_at) = row

            # Extract completed step IDs from validated_outputs keys
            try:
                if isinstance(validated_outputs_raw, dict):
                    completed = list(validated_outputs_raw.keys())
                elif isinstance(validated_outputs_raw, str):
                    completed = list(json.loads(validated_outputs_raw).keys())
                else:
                    completed = []
            except Exception:
                completed = []

            results.append({
                "run_id": run_id,
                "org_name": org_name,
                "mode": mode,
                "phase": phase,
                "pending_step_id": pending_step_id,
                "completed_steps": completed,
                "updated_at": updated_at.isoformat() if updated_at else None,
                "created_at": created_at.isoformat() if created_at else None,
            })
        return results
    except Exception as e:
        logger.warning("list_credentialing_runs failed: %s", e)
        return []


def get_latest_run_seed_for_org(org_name: str) -> dict[str, Any]:
    """Return seedable state from the most recent run for this org.

    Used to pre-populate a new run so users don't start from scratch each time.
    Returns a dict with keys like ``step3_roster_upload_id`` that can be injected
    into a fresh OrchestratorState.  Returns {} if nothing useful is found.
    """
    url = _db_url()
    if not url or not org_name:
        return {}
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                run_id,
                body->'orchestrator_state_dict'->>'step3_roster_upload_id' AS upload_id,
                body->'orchestrator_state_dict'->'org_npis'                AS org_npis,
                body->'orchestrator_state_dict'->'locations'               AS locations
            FROM credentialing_runs
            WHERE LOWER(body->>'org_name') = LOWER(%s)
              AND body->>'phase' != 'error'
              AND body->'orchestrator_state_dict' IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (org_name,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return {}
        run_id, upload_id, org_npis_raw, locations_raw = row
        seed: dict[str, Any] = {"_seeded_from_run_id": run_id}
        if upload_id:
            seed["step3_roster_upload_id"] = upload_id
        if org_npis_raw:
            try:
                npis = json.loads(org_npis_raw) if isinstance(org_npis_raw, str) else org_npis_raw
                if isinstance(npis, list):
                    seed["org_npis"] = npis
            except Exception:
                pass
        return seed
    except Exception as e:
        logger.warning("get_latest_run_seed_for_org failed: %s", e)
        return {}


def delete_credentialing_run(run_id: str) -> bool:
    """Hard-delete a credentialing run row. Returns True if a row was deleted."""
    url = _db_url()
    if not url:
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        cur = conn.cursor()
        cur.execute("DELETE FROM credentialing_runs WHERE run_id = %s", (run_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return deleted
    except Exception as e:
        logger.warning("delete_credentialing_run failed: %s", e)
        return False
