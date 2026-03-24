-- migrations/021_eval_tables.sql
-- Eval run storage for five-vector scoring rubric.
-- Run after migration 020_llm_analytics.sql

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id        TEXT        PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sprint_tag    TEXT        NOT NULL DEFAULT 'baseline',
    config_sha    TEXT,
    test_count    INTEGER     DEFAULT 0,
    pass_count    INTEGER     DEFAULT 0,
    partial_count INTEGER     DEFAULT 0,
    fail_count    INTEGER     DEFAULT 0,
    score_a       NUMERIC(5,2),
    score_b       NUMERIC(5,2),
    score_c       NUMERIC(5,2),
    score_d       NUMERIC(5,2),
    score_e       NUMERIC(5,2),
    score_overall NUMERIC(5,2),
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_ts
    ON eval_runs (ts DESC);
CREATE INDEX IF NOT EXISTS idx_eval_runs_sprint
    ON eval_runs (sprint_tag);

CREATE TABLE IF NOT EXISTS eval_results (
    result_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id             TEXT        NOT NULL REFERENCES eval_runs(run_id),
    ts                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Test identity
    test_id            TEXT,
    category           TEXT,
    vector             TEXT,
    question           TEXT,

    -- Routing
    tool_fired         TEXT,
    expected_tool      TEXT,
    tool_correct       BOOLEAN,
    iterations         INTEGER,
    latency_ms         INTEGER,

    -- Result
    result             TEXT,   -- completed | timeout | error
    adjudication_match BOOLEAN,
    adjudication_reason TEXT,

    -- Quality scores
    completeness       INTEGER,  -- 0-3
    task_completion    BOOLEAN,
    hallucination      BOOLEAN,
    actionable_escalation BOOLEAN,

    -- Signal flags
    json_bleed         BOOLEAN,
    repair_fired       BOOLEAN,
    legacy_path        BOOLEAN,
    streaming_gap_fail BOOLEAN,
    fallback_used      BOOLEAN,
    model_used         TEXT,

    -- Raw data
    thinking_log       TEXT,
    answer_preview     TEXT
);

CREATE INDEX IF NOT EXISTS idx_eval_results_run_id
    ON eval_results (run_id);
CREATE INDEX IF NOT EXISTS idx_eval_results_test_id
    ON eval_results (test_id);
CREATE INDEX IF NOT EXISTS idx_eval_results_category
    ON eval_results (category);
