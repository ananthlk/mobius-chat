-- J/P/D tagger support: policy lexicon + document tags replicated from RAG.
-- Synced by mobius-dbt sync_rag_lexicon_to_chat.py (same flow as published_rag_metadata).
-- document_id in document_tags aligns with published_rag_metadata.document_id (no FK; Chat has no documents table).

-- Lexicon metadata (single row for revision)
CREATE TABLE IF NOT EXISTS policy_lexicon_meta (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    revision BIGINT NOT NULL DEFAULT 0,
    lexicon_version VARCHAR(50) NOT NULL DEFAULT 'v1',
    lexicon_meta JSONB,
    created_at TIMESTAMPTZ DEFAULT (NOW() AT TIME ZONE 'utc'),
    updated_at TIMESTAMPTZ DEFAULT (NOW() AT TIME ZONE 'utc')
);

-- Lexicon entries (kind, code, spec with phrases for J/P/D tagging)
CREATE TABLE IF NOT EXISTS policy_lexicon_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind VARCHAR(10) NOT NULL,
    code VARCHAR(500) NOT NULL,
    parent_code VARCHAR(500),
    spec JSONB NOT NULL DEFAULT '{}',
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT (NOW() AT TIME ZONE 'utc'),
    updated_at TIMESTAMPTZ DEFAULT (NOW() AT TIME ZONE 'utc'),
    UNIQUE(kind, code)
);

CREATE INDEX IF NOT EXISTS idx_policy_lexicon_entries_kind_code ON policy_lexicon_entries(kind, code) WHERE active = true;

-- Document tags (J/P/D codes per document; used by retriever J/P/D tagger to scope BM25)
CREATE TABLE IF NOT EXISTS document_tags (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL UNIQUE,
    p_tags JSONB,
    d_tags JSONB,
    j_tags JSONB,
    lexicon_revision BIGINT,
    tagged_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_document_tags_document_id ON document_tags(document_id);
CREATE INDEX IF NOT EXISTS ix_document_tags_p_tags ON document_tags USING GIN (p_tags) WHERE p_tags IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_document_tags_d_tags ON document_tags USING GIN (d_tags) WHERE d_tags IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_document_tags_j_tags ON document_tags USING GIN (j_tags) WHERE j_tags IS NOT NULL;
