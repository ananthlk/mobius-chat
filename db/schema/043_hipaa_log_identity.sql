-- Migration 043: hipaa_analysis_log — solid-trace identity columns
--
-- HIPAA-policy ruling (PHI/HIPAA agent, 2026-07-19; directive from Ananth:
-- "the trace must be solid — store the org and user id etc"):
--   * correlation_id — answers "which event/turn": chat turn id (chat gate)
--     or ingest/job id (org-docs gate). Nullable by design (batch ingests may
--     lack one) but writers populate whenever their trigger has one.
--   * gate_source — discriminator for the two-DB trail (mobius_chat +
--     mobius_rag copies): a UNION query must know which gate wrote the row.
--   * org_source — separates gate-time-authoritative org resolution from
--     retro/unresolved rows: 'gate' (resolved live from the roster org
--     master at decision time) | 'unresolved' (no membership; org_slug
--     carries reserved '__unresolved__', never NULL, never a service name)
--     | 'backfill' (retro-resolved legacy row).
--
-- Org-of-record rule (writers): the UPLOADER'S org resolved AT GATE TIME
-- from user_org_membership (roster master). Service sentinels (e.g.
-- 'instant-rag') are retired entirely.
--
-- NOT NULL on user_id/org_slug is deliberately NOT added here — it lands in
-- a follow-up once both writers stamp per the rule (constraint-after-
-- compliance, same staging as the fact-store vocabulary).

ALTER TABLE compliance.hipaa_analysis_log
    ADD COLUMN IF NOT EXISTS correlation_id TEXT,
    ADD COLUMN IF NOT EXISTS gate_source    TEXT,
    ADD COLUMN IF NOT EXISTS org_source     TEXT;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='hipaa_log_gate_source_check') THEN
        ALTER TABLE compliance.hipaa_analysis_log ADD CONSTRAINT hipaa_log_gate_source_check
            CHECK (gate_source IS NULL OR gate_source IN ('chat_upload','org_docs_ingest'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='hipaa_log_org_source_check') THEN
        ALTER TABLE compliance.hipaa_analysis_log ADD CONSTRAINT hipaa_log_org_source_check
            CHECK (org_source IS NULL OR org_source IN ('gate','unresolved','backfill'));
    END IF;
END $$;

-- Lookup by event for full-event reconstruction (turn -> upload -> screening).
CREATE INDEX IF NOT EXISTS idx_hipaa_log_correlation
    ON compliance.hipaa_analysis_log (correlation_id)
    WHERE correlation_id IS NOT NULL;
