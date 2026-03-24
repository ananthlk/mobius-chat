"""Postgres persistence for credentialing co-pilot runs (shared across API + worker processes)."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _db_url() -> str:
    from app.chat_config import get_chat_config
    return (get_chat_config().rag.database_url or "").strip()


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
