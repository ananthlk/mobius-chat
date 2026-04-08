-- Instant-RAG metadata columns on published_rag_metadata.
-- Supports the instant-rag skill: ephemeral documents written directly
-- (bypassing dbt), with verification tier and agent scope tags.

ALTER TABLE published_rag_metadata ADD COLUMN IF NOT EXISTS instant_rag BOOLEAN DEFAULT FALSE;
ALTER TABLE published_rag_metadata ADD COLUMN IF NOT EXISTS verification_tier TEXT DEFAULT 'verified';
ALTER TABLE published_rag_metadata ADD COLUMN IF NOT EXISTS agent_scope_tags JSONB DEFAULT '[]';
ALTER TABLE published_rag_metadata ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- Partial indexes for instant-rag queries
CREATE INDEX IF NOT EXISTS idx_prm_instant_rag ON published_rag_metadata(instant_rag) WHERE instant_rag = TRUE;
CREATE INDEX IF NOT EXISTS idx_prm_expires_at ON published_rag_metadata(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_prm_agent_scope ON published_rag_metadata USING GIN (agent_scope_tags);
