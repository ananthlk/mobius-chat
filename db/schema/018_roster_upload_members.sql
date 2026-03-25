-- Normalized roster member rows for Step 3 (find associated providers) and reconciliation.
-- Written when POST /roster-uploads/{id}/process completes. Same DB as roster_uploads (CREDENTIALING_REPORT_DB_URL / CHAT_RAG_DATABASE_URL).

CREATE TABLE IF NOT EXISTS roster_upload_members (
    id BIGSERIAL PRIMARY KEY,
    upload_id TEXT NOT NULL,
    org_id TEXT,
    row_index INT NOT NULL DEFAULT 0,
    npi TEXT NOT NULL,
    display_name TEXT,
    address_line_1 TEXT,
    city TEXT,
    state TEXT,
    zip5 TEXT,
    source_row JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_roster_upload_members_upload ON roster_upload_members (upload_id);
CREATE INDEX IF NOT EXISTS idx_roster_upload_members_upload_npi ON roster_upload_members (upload_id, npi);
