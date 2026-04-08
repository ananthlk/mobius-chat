-- Financial strategy document + version model.
-- document_id = persistent per-org strategy handle (survives across sessions).
-- Each version = one draft→chat→refine→finalize cycle.
-- Same DB as chat_turns (CHAT_RAG_DATABASE_URL).

-- ── Documents: one per org, permanent handle ──────────────────────────────
CREATE TABLE IF NOT EXISTS financial_strategy_documents (
    document_id  TEXT PRIMARY KEY,
    org_name     TEXT NOT NULL,
    org_slug     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fsd_org ON financial_strategy_documents (LOWER(org_name));
CREATE INDEX IF NOT EXISTS idx_fsd_slug ON financial_strategy_documents (org_slug);

-- ── Versions: each refinement cycle under a document ──────────────────────
CREATE TABLE IF NOT EXISTS financial_strategy_versions (
    version_id   TEXT PRIMARY KEY,
    document_id  TEXT NOT NULL REFERENCES financial_strategy_documents(document_id) ON DELETE CASCADE,
    version_num  INT NOT NULL DEFAULT 1,
    thread_id    UUID REFERENCES chat_threads(thread_id) ON DELETE SET NULL,
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft', 'active', 'finalized', 'archived')),
    body         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- body schema (JSONB):
-- {
--   "baseline":       { ... full baseline snapshot at time of version ... },
--   "bookmarks":      [ { text, section, ts } ],
--   "chat_summary":   "auto-generated summary of chat trail",
--   "tasks_snapshot": [ { task_id, title, status, severity, tags } ],
--   "final_strategy": { report_md, verdict_overrides, user_notes },
--   "metadata":       { created_by, finalized_at, finalized_by }
-- }

CREATE INDEX IF NOT EXISTS idx_fsv_doc       ON financial_strategy_versions (document_id, version_num DESC);
CREATE INDEX IF NOT EXISTS idx_fsv_thread    ON financial_strategy_versions (thread_id);
CREATE INDEX IF NOT EXISTS idx_fsv_status    ON financial_strategy_versions (status, updated_at DESC);

-- Enforce unique version numbers per document
CREATE UNIQUE INDEX IF NOT EXISTS idx_fsv_doc_ver ON financial_strategy_versions (document_id, version_num);

COMMENT ON TABLE financial_strategy_documents IS
    'One per org. Permanent handle for the financial strategy. document_id appears in URLs.';
COMMENT ON TABLE financial_strategy_versions IS
    'Each refinement cycle: draft → active (chat/tasks) → finalized. Body holds full version state as JSONB.';
