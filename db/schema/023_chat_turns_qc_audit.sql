-- Quality-control audit metadata (eval adjudicator or future inline QC).
-- Surfaces in UI as a badge + optional thinking line.

ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS qc_audit JSONB;

COMMENT ON COLUMN chat_turns.qc_audit IS 'Optional { passed, reason, source, audited_at } after QC / eval adjudication';
