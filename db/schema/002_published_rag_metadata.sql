-- Published RAG metadata (from MOBIUS-DBT mart) for Mobius Chat.
-- Contract: MOBIUS-DBT/docs/CONTRACT_CHAT_CONSUMER.md
-- Metadata only (no embeddings in Postgres); embeddings live in Vertex AI Vector Search.
-- Link: id (PK) is the same id stored in Vertex; use for joining after vector search.

-- Metadata table: one row per published chunk/fact (same grain as BigQuery mart)
CREATE TABLE IF NOT EXISTS published_rag_metadata (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL,
    source_type TEXT NOT NULL,
    source_id UUID NOT NULL,
    model TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    text TEXT,
    page_number INT,
    paragraph_index INT,
    section_path TEXT,
    chapter_path TEXT,
    summary TEXT,
    document_filename TEXT,
    document_display_name TEXT,
    document_authority_level TEXT,
    document_effective_date TEXT,
    document_termination_date TEXT,
    document_payer TEXT,
    document_state TEXT,
    document_program TEXT,
    document_status TEXT,
    document_created_at TIMESTAMPTZ,
    document_review_status TEXT,
    document_reviewed_at TIMESTAMPTZ,
    document_reviewed_by TEXT,
    content_sha TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    source_verification_status TEXT
);

-- Indexes for filtering
CREATE INDEX IF NOT EXISTS idx_published_rag_metadata_document_id ON published_rag_metadata(document_id);
CREATE INDEX IF NOT EXISTS idx_published_rag_metadata_updated_at ON published_rag_metadata(updated_at);
CREATE INDEX IF NOT EXISTS idx_published_rag_metadata_payer ON published_rag_metadata(document_payer);
CREATE INDEX IF NOT EXISTS idx_published_rag_metadata_state ON published_rag_metadata(document_state);
CREATE INDEX IF NOT EXISTS idx_published_rag_metadata_program ON published_rag_metadata(document_program);
CREATE INDEX IF NOT EXISTS idx_published_rag_metadata_authority ON published_rag_metadata(document_authority_level);
CREATE INDEX IF NOT EXISTS idx_published_rag_metadata_generator_id ON published_rag_metadata(generator_id);

-- Sync run audit: one row per sync_mart_to_chat.py run
CREATE TABLE IF NOT EXISTS sync_runs (
    run_id UUID PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    mart_rows_read INT,
    postgres_rows_written INT,
    vector_rows_upserted INT,
    status TEXT NOT NULL,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_started_at ON sync_runs(started_at DESC);
