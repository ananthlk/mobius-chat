-- Unified credentialing assertions: org↔NPI, locations, provider–location links.
-- Versioning: one open row per (credentialing_run_id, subject_stable_key) via valid_to IS NULL.
-- Validate-only (same material_hash): bump validated_at. Material change: close prior row, insert new (same assertion_group_id).

CREATE TABLE IF NOT EXISTS credentialing_assertion (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    assertion_group_id UUID NOT NULL,
    credentialing_run_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT,
    org_name TEXT NOT NULL DEFAULT '',
    step_id TEXT NOT NULL,
    fact_kind TEXT NOT NULL CHECK (fact_kind IN ('org_npi', 'location', 'provider_link')),
    subject_stable_key TEXT NOT NULL,
    org_npi TEXT,
    location_id TEXT,
    provider_npi TEXT,
    location_address_snapshot TEXT,
    provider_name_snapshot TEXT,
    association_strength INT,
    rationales_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    material_hash TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'copilot' CHECK (mode IN ('copilot', 'autopilot')),
    status TEXT NOT NULL DEFAULT 'active',
    status_determined_by TEXT,
    policy_version TEXT,
    ruleset_hash TEXT,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ,
    validated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_credentialing_assertion_open
    ON credentialing_assertion (credentialing_run_id, subject_stable_key)
    WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_credentialing_assertion_run_step
    ON credentialing_assertion (credentialing_run_id, step_id);

CREATE INDEX IF NOT EXISTS idx_credentialing_assertion_org_open
    ON credentialing_assertion (org_name, step_id)
    WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_credentialing_assertion_group
    ON credentialing_assertion (assertion_group_id, valid_from);
