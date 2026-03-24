-- LLM call analytics (router / model_performance_by_stage / llm_analytics inserts).
-- Required before 022_model_performance_view.sql (materialized view reads llm_calls).

CREATE TABLE IF NOT EXISTS llm_calls (
    call_id            UUID PRIMARY KEY,
    correlation_id     TEXT,
    thread_id          TEXT,
    ts                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    config_sha         TEXT,
    model              TEXT NOT NULL DEFAULT 'unknown',
    provider           TEXT NOT NULL DEFAULT 'unknown',
    stage              TEXT NOT NULL DEFAULT 'unknown',
    tier               TEXT,
    complexity         TEXT,
    is_ab_call         BOOLEAN DEFAULT FALSE,
    ab_variant         TEXT,
    success            BOOLEAN NOT NULL DEFAULT TRUE,
    is_rate_limit      BOOLEAN DEFAULT FALSE,
    is_fallback        BOOLEAN DEFAULT FALSE,
    fallback_from      TEXT,
    completion_valid   BOOLEAN DEFAULT TRUE,
    error_type         TEXT,
    latency_ms         INTEGER,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cost_usd           NUMERIC(18, 8),
    quality_score      NUMERIC(8, 4),
    quality_source     TEXT,
    phi_detected       BOOLEAN DEFAULT FALSE,
    phi_scrubbed       BOOLEAN DEFAULT FALSE,
    phi_types          TEXT,
    prompt_len_chars   INTEGER,
    output_len_chars   INTEGER,
    prompt_hash        TEXT,
    synced_to_bq       BOOLEAN DEFAULT FALSE,
    synced_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls (ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_stage_model ON llm_calls (stage, model);
CREATE INDEX IF NOT EXISTS idx_llm_calls_correlation ON llm_calls (correlation_id);
