-- Roster review sessions (copilot/autopilot) and per-line user verdicts for find_associated_providers.
-- Requires pgcrypto for gen_random_uuid() (typical on Postgres 13+).

CREATE TABLE IF NOT EXISTS roster_review_session (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    credentialing_run_id TEXT,
    thread_id TEXT,
    org_name TEXT NOT NULL DEFAULT '',
    org_npis_json JSONB DEFAULT '[]'::jsonb,
    step_id TEXT NOT NULL DEFAULT 'find_associated_providers',
    mode TEXT NOT NULL DEFAULT 'copilot' CHECK (mode IN ('copilot', 'autopilot')),
    policy_version TEXT,
    ruleset_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_roster_review_session_run
    ON roster_review_session (credentialing_run_id)
    WHERE credentialing_run_id IS NOT NULL AND credentialing_run_id <> '';

CREATE INDEX IF NOT EXISTS idx_roster_review_session_org_created
    ON roster_review_session (org_name, created_at DESC);

CREATE TABLE IF NOT EXISTS roster_line_item (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES roster_review_session (id) ON DELETE CASCADE,
    stable_row_key TEXT NOT NULL,
    location_id TEXT NOT NULL DEFAULT '',
    location_address_snapshot TEXT,
    npi TEXT NOT NULL DEFAULT '',
    name_snapshot TEXT,
    model_score INT,
    model_rationale TEXT,
    user_verdict TEXT CHECK (
        user_verdict IS NULL
        OR user_verdict IN ('accept', 'reject', 'edit')
    ),
    user_note TEXT,
    edited_fields_json JSONB,
    source TEXT NOT NULL DEFAULT 'model' CHECK (source IN ('model', 'user_added')),
    sort_order INT NOT NULL DEFAULT 0,
    UNIQUE (session_id, stable_row_key)
);

CREATE INDEX IF NOT EXISTS idx_roster_line_item_session ON roster_line_item (session_id);
