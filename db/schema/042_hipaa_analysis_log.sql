-- Migration 042: compliance.hipaa_analysis_log — durable HIPAA-gate audit trail
--
-- P0 (Ananth 2026-07-17): EVERY upload transaction — clean / phi /
-- indeterminate — produces a durable HIPAA-analysis record: the proof the
-- platform screened the doc and what it did. Platform-side compliance mirror
-- of the PHI agent's own SHA-256/masked audit, and the source for the chat
-- bubble's DIAGNOSTICS section. Spec: docs/instant-rag-hipaa-gate-spec.md
-- ("HIPAA analysis audit + diagnostics"). Schema owner: Database agent.
--
-- Design invariants (Database agent, 2026-07-17):
--   * APPEND-ONLY. UPDATE/DELETE are blocked by trigger, not just convention —
--     audit integrity must not depend on application discipline.
--   * SURVIVES DOCUMENT PURGE. document_id is a bare identifier — deliberately
--     NO foreign key to instant_rag_uploads or any document table, so purging
--     a blocked PHI doc never cascades here (the row IS the "we caught it"
--     evidence).
--   * MASKED ONLY. evidence_categories carries category labels ("SSN","DOB",
--     "MRN"), never raw values. reason is free text authored by the
--     classifier — the PHI agent guarantees it is masked; nothing raw enters.
--   * Lives in its own `compliance` schema inside mobius_chat: co-located
--     with the writer (chat's gate) for transactional simplicity, namespaced
--     for permission separation and future isolation.
--
-- WRITE-PATH CONTRACT (chat implements as part of the gate):
--   * The audit INSERT happens in the SAME transaction as the gate decision,
--     and a failed audit write FAILS the gate CLOSED (block the upload): an
--     upload we cannot prove we screened must not proceed.
--   * id is CLIENT-generated (uuid4); retries INSERT ... ON CONFLICT (id)
--     DO NOTHING. The default is only a fallback for ad-hoc inserts.
--   * content_sha256 must match the PHI agent's audit hash so the two audit
--     trails correlate without sharing content.

CREATE SCHEMA IF NOT EXISTS compliance;

CREATE TABLE IF NOT EXISTS compliance.hipaa_analysis_log (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- What was screened
    transaction_id      TEXT NOT NULL,            -- upload transaction
    document_id         TEXT,                     -- null when blocked before registration
    content_sha256      TEXT,                     -- correlates with PHI agent's audit
    user_id             TEXT,
    org_slug            TEXT,

    -- The decision
    gate                TEXT NOT NULL
        CHECK (gate IN ('clean','phi','indeterminate')),
    phi_flag            BOOLEAN NOT NULL,
    ceiling             TEXT
        CHECK (ceiling IN ('private','org','public')),
    hipaa_mode_allowed  BOOLEAN NOT NULL,         -- mode at decision time
    action_taken        TEXT NOT NULL
        CHECK (action_taken IN
               ('published','published_private','blocked_phi','blocked_indeterminate')),

    -- The evidence (masked)
    evidence_categories TEXT[] NOT NULL DEFAULT '{}',  -- e.g. {SSN,DOB,MRN}
    classifier_version  TEXT NOT NULL,
    layers_run          TEXT[] NOT NULL DEFAULT '{}',
    confidence          DOUBLE PRECISION,
    reason              TEXT
);

-- Diagnostics reads: per-transaction / per-document lookup + org timeline.
CREATE INDEX IF NOT EXISTS idx_hipaa_log_txn
    ON compliance.hipaa_analysis_log (transaction_id);
CREATE INDEX IF NOT EXISTS idx_hipaa_log_doc
    ON compliance.hipaa_analysis_log (document_id)
    WHERE document_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_hipaa_log_org_ts
    ON compliance.hipaa_analysis_log (org_slug, ts DESC);

-- Append-only enforcement: trigger beats GRANT discipline (fires for every
-- role; roles/owners change, this doesn't).
CREATE OR REPLACE FUNCTION compliance.hipaa_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'compliance.hipaa_analysis_log is append-only (audit record)';
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_hipaa_log_immutable ON compliance.hipaa_analysis_log;
CREATE TRIGGER trg_hipaa_log_immutable
    BEFORE UPDATE OR DELETE ON compliance.hipaa_analysis_log
    FOR EACH ROW EXECUTE FUNCTION compliance.hipaa_log_immutable();

REVOKE UPDATE, DELETE ON compliance.hipaa_analysis_log FROM PUBLIC;
