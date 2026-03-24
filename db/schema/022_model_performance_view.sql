-- migrations/022_model_performance_view.sql
-- Materialized view driving ModelRouter dynamic selection.
-- Refreshed every 5 minutes via pg_cron or manual REFRESH.
-- Depends on llm_calls table (migration 020).

CREATE MATERIALIZED VIEW IF NOT EXISTS model_performance_by_stage AS
SELECT
    stage,
    model,

    -- Volume
    COUNT(*)                                                    AS total_calls,
    COUNT(quality_score)                                        AS quality_samples,

    -- Hard reliability (circuit breaker inputs)
    AVG(CASE WHEN success = false
             THEN 1.0 ELSE 0.0 END)                            AS hard_error_rate,
    AVG(CASE WHEN error_type IS NOT NULL
             THEN 1.0 ELSE 0.0 END)                            AS any_error_rate,
    AVG(CASE WHEN is_rate_limit = true
             THEN 1.0 ELSE 0.0 END)                            AS rate_limit_rate,

    -- Recent error spike (24h window — circuit breaker)
    AVG(CASE WHEN error_type IS NOT NULL
             AND ts > NOW() - INTERVAL '24 hours'
             THEN 1.0 ELSE 0.0 END)                            AS error_rate_24h,

    -- Performance (successful calls only)
    AVG(latency_ms)     FILTER (WHERE success = true)          AS avg_latency_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)
                        FILTER (WHERE success = true)          AS p95_latency_ms,
    AVG(cost_usd)       FILTER (WHERE success = true)          AS avg_cost_usd,

    -- Quality (adjudicated calls only)
    AVG(quality_score)  FILTER (WHERE quality_score IS NOT NULL) AS avg_quality,
    STDDEV(quality_score) FILTER (WHERE quality_score IS NOT NULL) AS quality_stddev,
    MIN(quality_score)  FILTER (WHERE quality_score IS NOT NULL) AS min_quality,
    MAX(quality_score)  FILTER (WHERE quality_score IS NOT NULL) AS max_quality,

    -- Recency
    MAX(ts)                                                     AS last_used,

    -- Confidence tier (computed)
    CASE
        WHEN COUNT(quality_score) >= 100 THEN 'locked'
        WHEN COUNT(quality_score) >= 50  THEN 'high'
        WHEN COUNT(quality_score) >= 10  THEN 'medium'
        ELSE 'low'
    END                                                         AS confidence

FROM llm_calls
WHERE ts > NOW() - INTERVAL '30 days'
GROUP BY stage, model

WITH DATA;

-- Unique index required for REFRESH CONCURRENTLY
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_perf_stage_model
    ON model_performance_by_stage (stage, model);

CREATE INDEX IF NOT EXISTS idx_model_perf_stage
    ON model_performance_by_stage (stage);

-- Manual refresh (call after migration and every 5 min via pg_cron)
-- REFRESH MATERIALIZED VIEW CONCURRENTLY model_performance_by_stage;

-- Optional: pg_cron job (requires pg_cron extension)
-- SELECT cron.schedule(
--     'refresh-model-perf',
--     '*/5 * * * *',
--     'REFRESH MATERIALIZED VIEW CONCURRENTLY model_performance_by_stage'
-- );


-- Helper view: current winner per stage (useful for admin dashboard)
CREATE OR REPLACE VIEW model_winner_by_stage AS
SELECT DISTINCT ON (stage)
    stage,
    model,
    confidence,
    avg_quality,
    avg_latency_ms,
    avg_cost_usd,
    hard_error_rate,
    total_calls,
    quality_samples
FROM model_performance_by_stage
WHERE confidence IN ('high', 'locked')
  AND hard_error_rate < 0.10
  AND avg_quality IS NOT NULL
ORDER BY stage, avg_quality DESC;


-- Scoring query for admin (legacy flat 15s / $0.05 caps).
-- Live router + hamburger report use app.services.model_registry.composite_router_signal
-- (per-stage linear caps + per-call token list price). Refresh this view if you need SQL parity.
CREATE OR REPLACE VIEW model_composite_scores AS
SELECT
    stage,
    model,
    confidence,
    total_calls,
    quality_samples,
    ROUND(avg_quality::numeric, 3)      AS quality,
    ROUND(hard_error_rate::numeric, 3)  AS error_rate,
    ROUND(avg_latency_ms::numeric)      AS latency_ms,
    ROUND(avg_cost_usd::numeric, 5)     AS cost_usd,

    -- Composite score (legacy flat caps; mirrors Python router 25/25/25/25 blend)
    ROUND((
        COALESCE(avg_quality, 0.5) * 0.25

        + GREATEST(0, 1.0 - hard_error_rate * 2.0) * 0.25

        + GREATEST(0, 1.0 - LEAST(p95_latency_ms, 15000) / 15000.0) * 0.25

        + GREATEST(0, 1.0 - LEAST(avg_cost_usd, 0.05) / 0.05) * 0.25
    )::numeric, 3)                      AS composite_score

FROM model_performance_by_stage
ORDER BY stage, composite_score DESC NULLS LAST;
