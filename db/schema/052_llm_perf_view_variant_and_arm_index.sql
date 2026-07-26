-- Migration 052: variant-aware arm index + perf-view recreate (Migration B).
--
-- Part of the Chat pipeline refactor (LLMManager). GATED on Migration A (051):
--   run  SELECT COUNT(*) FROM llm_calls WHERE variant_id IS NULL;
--   and record 0 in the migration tracker BEFORE applying this file (AC-18).
-- If any row still has NULL variant_id, STOP — the arm grain would be wrong.
--
-- Authoritative spec: mobius-chat/docs/SPEC_LLM_MANAGER.md §2.4.
-- Corrects three defects from the original gate-doc DDL:
--   * arm-key index is NON-UNIQUE (llm_calls is a per-call log; a UNIQUE arm
--     index would reject the 2nd call on any arm).
--   * arm key uses `model` (fully populated), NOT `ab_variant` (legacy/sparse).
--   * model_performance_by_stage is a MATERIALIZED view — cannot be ALTERed or
--     CREATE-OR-REPLACEd; must DROP ... CASCADE (drops the two dependent views)
--     then recreate all three, adding variant_id to the grain.
--
-- ⚠️ REQUIRED COUPLED CODE CHANGE — MUST DEPLOY WITH THIS MIGRATION:
--   app/services/model_registry.py:~1956 reads this view as
--     new_stats[stage][model] = dict(row)
--   With variant_id in the grain there is now one row per (stage, model, variant_id).
--   That assignment would OVERWRITE per-variant rows (last variant wins), silently
--   corrupting model-stat aggregation used for live model selection.
--   FIX (must land in the same deploy): the model_registry read must add
--     WHERE variant_id = 'default'
--   so model selection reads the default-prompt slice (consistent with the §5
--   mutual-exclusion design: model exploration happens on default-variant turns).
--   LATENT in v1 (all rows are 'default' → one row per (stage, model), no breakage
--   yet) but it becomes a live bug the instant a non-default variant is written.
--   Do NOT land 052 without the model_registry read change.

-- ── llm_calls indexes ────────────────────────────────────────────────────────

-- Arm-grouped reads. Non-unique. Matches the ratified arm key (module_key, model,
-- variant_id). ab_variant is intentionally NOT part of the key.
CREATE INDEX IF NOT EXISTS idx_llm_calls_arm_key
    ON llm_calls (module_key, model, variant_id);

-- Eval attribution join key (per-module reward/eval linkage). Without it every
-- attribution query seq-scans a table that grows per LLM call.
CREATE INDEX IF NOT EXISTS idx_llm_calls_attribution
    ON llm_calls (turn_id, module_key);

-- ── model_performance_by_stage (matview) + dependents ────────────────────────
-- CASCADE also drops model_winner_by_stage + model_composite_scores; recreated below.
DROP MATERIALIZED VIEW IF EXISTS model_performance_by_stage CASCADE;

CREATE MATERIALIZED VIEW IF NOT EXISTS model_performance_by_stage AS
SELECT
    stage,
    model,
    variant_id,                                                 -- NEW: variant dimension

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
GROUP BY stage, model, variant_id                              -- NEW: + variant_id

WITH DATA;

-- Unique index (now includes variant_id) — required for REFRESH CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_perf_stage_model
    ON model_performance_by_stage (stage, model, variant_id);

CREATE INDEX IF NOT EXISTS idx_model_perf_stage
    ON model_performance_by_stage (stage);

-- REFRESH MATERIALIZED VIEW CONCURRENTLY model_performance_by_stage;  -- run post-migration + every 5 min

-- Helper view: current winner per stage (admin dashboard).
-- Behavior-preserving: filtered to variant_id='default' so admin winner reflects
-- the production prompt (identical to pre-migration output while all data is default).
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
  AND variant_id = 'default'
ORDER BY stage, avg_quality DESC;

-- Scoring query for admin (legacy flat 15s / $0.05 caps).
-- Behavior-preserving: filtered to variant_id='default' (see above).
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
WHERE variant_id = 'default'
ORDER BY stage, composite_score DESC NULLS LAST;
