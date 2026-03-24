-- migrations/020_llm_analytics.sql
-- Adds: llm_calls, llm_config_versions, llm_quality_updates, phi_audit_log
-- Run after 013-019. Same DB as chat_turns (CHAT_RAG_DATABASE_URL).
-- Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS.

-- ── llm_calls ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id    TEXT,
    thread_id         TEXT,
    ts                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    config_sha        TEXT,

    model             TEXT        NOT NULL,
    provider          TEXT        NOT NULL,
    stage             TEXT        NOT NULL,
    tier              TEXT,
    complexity        TEXT,
    is_ab_call        BOOLEAN     DEFAULT FALSE,
    ab_variant        TEXT,

    success           BOOLEAN     NOT NULL,
    is_rate_limit     BOOLEAN     DEFAULT FALSE,
    is_fallback       BOOLEAN     DEFAULT FALSE,
    fallback_from     TEXT,
    completion_valid  BOOLEAN     DEFAULT TRUE,
    error_type        TEXT,

    latency_ms        INTEGER,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    total_tokens      INTEGER GENERATED ALWAYS AS
                      (COALESCE(input_tokens,0)+COALESCE(output_tokens,0)) STORED,

    cost_usd          NUMERIC(12,8),

    quality_score     NUMERIC(4,3),
    quality_source    TEXT,

    phi_detected      BOOLEAN     DEFAULT FALSE,
    phi_scrubbed      BOOLEAN     DEFAULT FALSE,
    phi_types         TEXT,

    prompt_len_chars  INTEGER,
    output_len_chars  INTEGER,
    prompt_hash       TEXT,

    synced_to_bq      BOOLEAN     DEFAULT FALSE,
    synced_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_ts
    ON llm_calls (ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_config_sha
    ON llm_calls (config_sha);
CREATE INDEX IF NOT EXISTS idx_llm_calls_stage_model
    ON llm_calls (stage, model);
CREATE INDEX IF NOT EXISTS idx_llm_calls_correlation
    ON llm_calls (correlation_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_unsynced
    ON llm_calls (synced_to_bq, ts) WHERE synced_to_bq = FALSE;


-- ── llm_config_versions ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_config_versions (
    config_sha        TEXT        PRIMARY KEY,
    config_json       JSONB       NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by        TEXT,
    notes             TEXT,
    model             TEXT,
    provider          TEXT,
    prompt_count      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_llm_config_versions_ts
    ON llm_config_versions (created_at DESC);


-- ── llm_quality_updates ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_quality_updates (
    update_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id           UUID        NOT NULL REFERENCES llm_calls(call_id),
    ts                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    quality_score     NUMERIC(4,3) NOT NULL,
    quality_source    TEXT         NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_call_id
    ON llm_quality_updates (call_id);


-- ── phi_audit_log (HIPAA PHI audit trail) ───────────────────────────
CREATE TABLE IF NOT EXISTS phi_audit_log (
    event_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    ts                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    correlation_id    TEXT,
    thread_id         TEXT,
    event_type        TEXT        NOT NULL,
    phi_types         TEXT,
    phi_count         INTEGER,
    stage             TEXT,
    model_used        TEXT,
    action_taken      TEXT        NOT NULL,
    hipaa_mode_active BOOLEAN     DEFAULT FALSE,
    baa_available     BOOLEAN     DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_phi_audit_log_ts
    ON phi_audit_log (ts DESC);
CREATE INDEX IF NOT EXISTS idx_phi_audit_log_correlation
    ON phi_audit_log (correlation_id);
