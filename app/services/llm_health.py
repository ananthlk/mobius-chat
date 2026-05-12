"""Postgres-backed live-health detector for the bandit's circuit breaker.

This module is the short-window counterpart to model_performance_by_stage
(the 30-day matview that drives steady-state quality/cost/latency
priors). The matview is too coarse to catch a 5-minute backend
slowdown — we saw this on 2026-04-28 when Vertex flash timed out
three turns back-to-back at 45s each but the 24h error_rate row
barely moved.

Design:

* Source of truth: ``llm_calls`` table (every LLM call already
  records model, stage, latency_ms, success, error_type, ts).
* View: ``model_health_recent`` (migration 034) aggregates the
  last 5 minutes from ``llm_calls`` per (model, stage).
* This module: a daemon thread polls the view every 10 seconds
  and caches the result in memory. ``is_degraded()`` reads the
  cache (sub-millisecond) so the bandit's per-call routing stays
  fast.

All chat instances poll the same view from the same Postgres,
so degradation signal is naturally consistent across instances —
no Redis broadcast, no probe-lock dance, no per-instance state
divergence.

Failure mode: if Postgres is unreachable, ``is_degraded()`` returns
False (fail-open). The bandit's normal routing takes over; we'd
rather route to a possibly-slow model than degrade everything to
its fallback chain.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# How often the refresher thread queries the view. 10s is fast enough
# that a 3-timeout burst (~30s elapsed) reliably enters the cache before
# the bandit picks the next model, and slow enough that the query cost
# is trivial (~6 reads/min/instance × 30ms = sub-second total work).
_REFRESH_INTERVAL_S = float(os.environ.get("LLM_HEALTH_REFRESH_INTERVAL_S", "10") or 10)

# Degradation thresholds. Conservative defaults; tunable via env so we
# can dial in once we have production data without a redeploy.
_FAIL_THRESHOLD = int(os.environ.get("LLM_HEALTH_FAIL_THRESHOLD", "2") or 2)
_MIN_TOTAL      = int(os.environ.get("LLM_HEALTH_MIN_TOTAL", "2") or 2)
_LATENCY_RATIO  = float(os.environ.get("LLM_HEALTH_LATENCY_RATIO", "3.0") or 3.0)


@dataclass
class _ModelHealthRow:
    model: str
    stage: str
    recent_total: int
    recent_timeouts: int
    recent_failures: int
    recent_avg_latency_ms: float | None
    recent_p95_latency_ms: float | None


class LlmHealthState:
    """Singleton state for the live-health detector.

    Holds the latest snapshot of ``model_health_recent`` in memory.
    Bandit calls ``is_degraded(model_id)`` on every routing decision;
    this returns from cache without touching Postgres.

    A background daemon thread (started via ``start()``) refreshes
    the cache every ``_REFRESH_INTERVAL_S`` seconds.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # model_id -> {reason, since_seen_at, last_refresh}
        self._degraded: dict[str, dict[str, Any]] = {}
        # model_id -> aggregated row across stages (we degrade per
        # MODEL, not per stage — if flash is broken, it's broken
        # for every stage that uses it)
        self._latest: dict[str, _ModelHealthRow] = {}
        self._last_refresh_ts: float = 0.0
        self._last_refresh_ok: bool = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── Public API ────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background refresher daemon. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        if (os.environ.get("LLM_HEALTH_DISABLED") or "").strip().lower() in ("1", "true", "yes"):
            logger.info("llm-health: disabled via LLM_HEALTH_DISABLED env")
            return
        self._stop_event.clear()
        t = threading.Thread(
            target=self._refresh_loop, name="llm-health-refresher", daemon=True
        )
        self._thread = t
        t.start()
        logger.info(
            "llm-health: refresher started (interval=%.1fs, fail_threshold=%d, min_total=%d, latency_ratio=%.1f)",
            _REFRESH_INTERVAL_S, _FAIL_THRESHOLD, _MIN_TOTAL, _LATENCY_RATIO,
        )

    def stop(self) -> None:
        """Stop the refresher (used in tests)."""
        self._stop_event.set()

    def is_degraded(self, model_id: str) -> bool:
        with self._lock:
            return model_id in self._degraded

    def degradation_reason(self, model_id: str) -> str:
        with self._lock:
            entry = self._degraded.get(model_id)
            return entry["reason"] if entry else ""

    def snapshot(self) -> dict[str, Any]:
        """For the /admin/model-health endpoint."""
        now = time.time()
        with self._lock:
            return {
                "source": "postgres:model_health_recent",
                "last_refresh_age_s": now - self._last_refresh_ts if self._last_refresh_ts else None,
                "last_refresh_ok": self._last_refresh_ok,
                "thresholds": {
                    "fail_threshold": _FAIL_THRESHOLD,
                    "min_total": _MIN_TOTAL,
                    "latency_ratio": _LATENCY_RATIO,
                    "refresh_interval_s": _REFRESH_INTERVAL_S,
                },
                "degraded": {
                    mid: {
                        "reason": e["reason"],
                        "first_seen_s_ago": now - e["since_seen_at"],
                    }
                    for mid, e in self._degraded.items()
                },
                "models": {
                    mid: {
                        "total": r.recent_total,
                        "timeouts": r.recent_timeouts,
                        "failures": r.recent_failures,
                        "avg_latency_ms": r.recent_avg_latency_ms,
                        "p95_latency_ms": r.recent_p95_latency_ms,
                    }
                    for mid, r in self._latest.items()
                },
            }

    # ── Internals ─────────────────────────────────────────────────────

    def _refresh_loop(self) -> None:
        # Stagger the first refresh so multiple instances starting
        # together don't all query Postgres at the exact same instant.
        import random as _rand
        time.sleep(_rand.uniform(0, _REFRESH_INTERVAL_S))
        while not self._stop_event.is_set():
            try:
                self._refresh_once()
            except Exception as e:
                logger.warning("llm-health: refresh failed (non-fatal): %s", e)
                with self._lock:
                    self._last_refresh_ok = False
            self._stop_event.wait(_REFRESH_INTERVAL_S)

    def _refresh_once(self) -> None:
        rows = self._query_health_view()
        if rows is None:
            return
        # Aggregate per-model across stages — a degraded backend is
        # degraded for every stage that uses it. Sum the counts, take
        # max latency.
        per_model: dict[str, _ModelHealthRow] = {}
        for r in rows:
            existing = per_model.get(r.model)
            if existing is None:
                per_model[r.model] = r
                continue
            per_model[r.model] = _ModelHealthRow(
                model=r.model,
                stage="",  # aggregated across stages
                recent_total=existing.recent_total + r.recent_total,
                recent_timeouts=existing.recent_timeouts + r.recent_timeouts,
                recent_failures=existing.recent_failures + r.recent_failures,
                recent_avg_latency_ms=max(
                    existing.recent_avg_latency_ms or 0.0,
                    r.recent_avg_latency_ms or 0.0,
                ),
                recent_p95_latency_ms=max(
                    existing.recent_p95_latency_ms or 0.0,
                    r.recent_p95_latency_ms or 0.0,
                ),
            )

        # Compare to ema_latency from the in-memory MODEL_ROSTER (set by
        # the bandit's update_ema). This gives us the "abnormally slow"
        # signal — model is responding but slower than its baseline.
        try:
            from app.services.model_registry import MODEL_ROSTER
            ema_lookup = {mid: spec.ema_latency_ms for mid, spec in MODEL_ROSTER.items()}
        except Exception:
            ema_lookup = {}

        new_degraded: dict[str, dict[str, Any]] = {}
        now = time.time()
        for model_id, row in per_model.items():
            reason = self._evaluate(row, ema_lookup.get(model_id, 0.0))
            if reason is None:
                continue
            with self._lock:
                prev = self._degraded.get(model_id)
            since = prev["since_seen_at"] if prev else now
            new_degraded[model_id] = {"reason": reason, "since_seen_at": since}

        with self._lock:
            transitioned_to: list[tuple[str, str]] = []
            transitioned_from: list[str] = []
            for mid, info in new_degraded.items():
                if mid not in self._degraded:
                    transitioned_to.append((mid, info["reason"]))
            for mid in list(self._degraded.keys()):
                if mid not in new_degraded:
                    transitioned_from.append(mid)
            self._degraded = new_degraded
            self._latest = per_model
            self._last_refresh_ts = now
            self._last_refresh_ok = True

        for mid, reason in transitioned_to:
            logger.warning("llm-health: model=%s DEGRADED — %s", mid, reason)
        for mid in transitioned_from:
            logger.info("llm-health: model=%s recovered (no longer in degraded set)", mid)

    # Stages expected to produce long outputs — latency-ratio check would
    # fire false positives because their 20-40s generation time is normal,
    # not a sign of backend degradation. Only timeout/error-rate checks apply.
    _LONG_OUTPUT_STAGE_PREFIXES: tuple[str, ...] = ("appeals_", "credentialing_", "integrator_roster")

    @staticmethod
    def _evaluate(row: _ModelHealthRow, ema_latency_ms: float) -> str | None:
        """Return a degradation reason string, or None if healthy."""
        if row.recent_total < _MIN_TOTAL:
            return None
        if row.recent_timeouts >= _FAIL_THRESHOLD:
            return f"{row.recent_timeouts}/{row.recent_total} recent calls timed out"
        # Latency-deviation trigger — skip for long-output stages where slow
        # generation is expected (not a sign of backend degradation).
        stage = (row.stage or "")
        is_long_output = any(stage.startswith(p) for p in LLMHealthMonitor._LONG_OUTPUT_STAGE_PREFIXES)
        if (
            not is_long_output
            and ema_latency_ms > 0
            and row.recent_avg_latency_ms
            and row.recent_avg_latency_ms > _LATENCY_RATIO * ema_latency_ms
        ):
            return (
                f"recent avg {row.recent_avg_latency_ms:.0f}ms > "
                f"{_LATENCY_RATIO:.1f}× ema {ema_latency_ms:.0f}ms"
            )
        return None

    def _query_health_view(self) -> list[_ModelHealthRow] | None:
        """Run the SELECT against ``model_health_recent``.

        Returns None on connection failure; caller treats that as
        "no update this cycle, keep prior cache."
        """
        try:
            from app.db_client import _acquire_conn, _release_conn, _get_fallback_url
        except Exception:
            return None
        url = _get_fallback_url("chat")
        if not url:
            return None
        sql = """
            SELECT model, stage, recent_total, recent_timeouts, recent_failures,
                   recent_avg_latency_ms, recent_p95_latency_ms
            FROM model_health_recent
        """
        try:
            conn, is_pooled = _acquire_conn(url)
        except Exception as e:
            logger.debug("llm-health: acquire_conn failed: %s", e)
            return None
        broken = False
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                fetched = cur.fetchall()
        except Exception as e:
            broken = True
            # First-run case: view doesn't exist yet (migration not
            # applied). Log clearly so ops can fix; degrade detection
            # is no-op until then.
            msg = str(e)
            if "does not exist" in msg.lower() or "undefined_table" in msg.lower():
                logger.warning(
                    "llm-health: model_health_recent view missing — "
                    "run migration db/schema/034_model_health_recent.sql"
                )
            else:
                logger.warning("llm-health: query failed: %s", e)
            return None
        finally:
            _release_conn(url, conn, is_pooled, is_broken=broken)

        out: list[_ModelHealthRow] = []
        for r in fetched:
            try:
                out.append(_ModelHealthRow(
                    model=str(r[0]),
                    stage=str(r[1]) if r[1] else "",
                    recent_total=int(r[2] or 0),
                    recent_timeouts=int(r[3] or 0),
                    recent_failures=int(r[4] or 0),
                    recent_avg_latency_ms=float(r[5]) if r[5] is not None else None,
                    recent_p95_latency_ms=float(r[6]) if r[6] is not None else None,
                ))
            except Exception:
                continue
        return out


# Module-level singleton.
LIVE_HEALTH = LlmHealthState()
