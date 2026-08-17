"""Periodic refresh of the doc-grain materialized view (migration 059).

``published_rag_documents`` collapses the chunk-grain
``published_rag_metadata`` (~1.95M rows) to one row per document (~9210).
It powers fetch_document's name-match tier at ~30x the speed of the base
table — but a materialized view is a CACHE: it does not see newly
published documents until refreshed. This runs
``REFRESH MATERIALIZED VIEW CONCURRENTLY`` on a ~10-min cadence so new
docs become name-matchable within one window. (fetch_document also falls
back to the live base table on an MV miss, covering the in-window gap, so
freshness is never all-or-nothing.)

CONCURRENTLY = non-blocking: readers keep hitting the current snapshot
during the ~6s rebuild. A Postgres session-level advisory lock ensures
only ONE instance refreshes at a time when Cloud Run runs several — the
lock is released when the short-lived connection closes, so a crashed
refresher never wedges the others.

Env:
  CHAT_DOCGRAIN_REFRESH_DISABLED=1   — turn the loop off entirely
  CHAT_DOCGRAIN_REFRESH_SECONDS=600  — cadence override (min 60)
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_VIEW = "published_rag_documents"
# Stable, arbitrary application-wide key so all instances contend on the
# same advisory lock for this specific refresh.
_ADVISORY_LOCK_KEY = 0x0D0C6A17
_DEFAULT_INTERVAL_S = 600
_INITIAL_DELAY_S = 20


class _DocGrainRefresher:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        if (os.environ.get("CHAT_DOCGRAIN_REFRESH_DISABLED") or "").strip().lower() in ("1", "true", "yes"):
            logger.info("docgrain-refresh: disabled via CHAT_DOCGRAIN_REFRESH_DISABLED")
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._loop, name="docgrain-refresh", daemon=True
        )
        self._thread.start()
        logger.info("docgrain-refresh: started (interval=%ss)", self._interval())

    def _interval(self) -> int:
        try:
            return max(60, int(os.environ.get("CHAT_DOCGRAIN_REFRESH_SECONDS", _DEFAULT_INTERVAL_S)))
        except (ValueError, TypeError):
            return _DEFAULT_INTERVAL_S

    def _loop(self) -> None:
        # Let startup (migrations, worker warm-up) settle before the first
        # refresh so we don't compete with the initial MV build.
        time.sleep(_INITIAL_DELAY_S)
        while True:
            try:
                self.refresh_once()
            except Exception as exc:  # never let the loop die
                logger.warning("docgrain-refresh: cycle failed (non-fatal): %s", exc)
            time.sleep(self._interval())

    def refresh_once(self) -> bool:
        """Refresh the MV under an advisory lock. Returns True if THIS
        call performed the refresh, False if it skipped (lock held
        elsewhere, no DB URL, or REFRESH failed)."""
        try:
            import psycopg2
            from app.db_client import _get_fallback_url
        except Exception as exc:  # pragma: no cover — import guard
            logger.warning("docgrain-refresh: import failed: %s", exc)
            return False

        url = _get_fallback_url("chat")
        if not url:
            logger.debug("docgrain-refresh: no chat DB URL; skipping")
            return False

        conn = None
        try:
            conn = psycopg2.connect(url)
            # REFRESH ... CONCURRENTLY cannot run inside a transaction block.
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
                got = cur.fetchone()[0]
                if not got:
                    logger.debug("docgrain-refresh: another instance holds the lock; skipping")
                    return False
                try:
                    t0 = time.monotonic()
                    cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {_VIEW}")
                    logger.info(
                        "docgrain-refresh: refreshed %s in %.1fs",
                        _VIEW, time.monotonic() - t0,
                    )
                    return True
                except psycopg2.Error as exc:
                    # MV absent (migration 059 not applied) or not
                    # CONCURRENTLY-refreshable yet — log and move on.
                    logger.warning("docgrain-refresh: REFRESH failed (non-fatal): %s", exc)
                    return False
                finally:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
        except Exception as exc:
            logger.warning("docgrain-refresh: connection/refresh error (non-fatal): %s", exc)
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


REFRESHER = _DocGrainRefresher()
