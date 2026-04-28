-- Live-health view for the bandit's short-window circuit breaker.
--
-- The existing model_performance_by_stage matview (migration 022)
-- aggregates over 30 days and is refreshed every 5 minutes — the
-- right granularity for steady-state quality/cost decisions but too
-- coarse to catch a 5-minute backend slowdown. We saw this on
-- 2026-04-28: Vertex flash timed out three turns back-to-back at
-- 45s each, but the 24h error-rate row barely moved.
--
-- This is a regular VIEW (not materialized) — recomputed on every
-- read. The query is cheap because llm_calls has an index on ts.
-- A background refresher thread in each chat instance polls this
-- every 10s into an in-memory cache, so routing decisions stay
-- sub-ms while the underlying data is consistent across instances
-- (single source of truth = llm_calls).
--
-- Counterpart in code: app/services/llm_health.py

CREATE OR REPLACE VIEW model_health_recent AS
SELECT
    model,
    stage,
    COUNT(*)                                                          AS recent_total,
    COUNT(*) FILTER (
        WHERE error_type IS NOT NULL
          AND (error_type ILIKE '%timeout%' OR error_type = 'TimeoutError')
    )                                                                 AS recent_timeouts,
    COUNT(*) FILTER (WHERE NOT success)                               AS recent_failures,
    AVG(latency_ms) FILTER (WHERE success)                            AS recent_avg_latency_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)
                          FILTER (WHERE success)                      AS recent_p95_latency_ms,
    MAX(ts)                                                           AS last_call_ts,
    NOW()                                                             AS computed_at
FROM llm_calls
WHERE ts > NOW() - INTERVAL '5 minutes'
GROUP BY model, stage;

-- Operators can hit this view directly to see live degradation:
--   SELECT * FROM model_health_recent
--   WHERE recent_timeouts >= 2
--   ORDER BY last_call_ts DESC;
