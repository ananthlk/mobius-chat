-- Line-level J/P/D tags for reranker (from policy_lines in RAG).
-- Synced by mobius-dbt sync_rag_lexicon_to_chat.py from policy_lines.
-- Key: (document_id, normalized_text) matches chunk lookup for line-level tag_match.

CREATE TABLE IF NOT EXISTS policy_line_tags (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL,
    normalized_text TEXT NOT NULL,
    p_tags JSONB,
    d_tags JSONB,
    j_tags JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(document_id, normalized_text)
);

CREATE INDEX IF NOT EXISTS ix_policy_line_tags_document_id ON policy_line_tags(document_id);
CREATE INDEX IF NOT EXISTS ix_policy_line_tags_doc_norm ON policy_line_tags(document_id, normalized_text);
