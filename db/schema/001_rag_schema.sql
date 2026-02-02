-- Cloned RAG schema for Mobius-Chat (read-only use; when docs are published they move here).
-- Matches RAG: documents, chunks, facts, chunk_embeddings. Uses pgvector for semantic search.

CREATE EXTENSION IF NOT EXISTS vector;

-- Documents (source PDFs, etc.)
CREATE TABLE IF NOT EXISTS documents (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    source_type TEXT DEFAULT 'document',
    created_at  TIMESTAMPTZ DEFAULT now(),
    metadata    JSONB DEFAULT '{}'
);

-- Chunks (text segments per document, optional page)
CREATE TABLE IF NOT EXISTS chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    page_number INT,
    start_offset INT,
    end_offset   INT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    metadata    JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);

-- Chunk embeddings (one embedding per chunk; same model = same dimensions, e.g. 768 for text-embedding-004)
CREATE TABLE IF NOT EXISTS chunk_embeddings (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id   UUID NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    embedding  vector(768) NOT NULL,
    model_id   TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_chunk_id ON chunk_embeddings(chunk_id);
-- HNSW index for fast approximate nearest-neighbor search (cosine distance)
CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_embedding ON chunk_embeddings
    USING hnsw (embedding vector_cosine_ops);

-- Facts (optional; for facts-based retrieval later)
CREATE TABLE IF NOT EXISTS facts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
    chunk_id    UUID REFERENCES chunks(id) ON DELETE SET NULL,
    fact_type   TEXT,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    metadata    JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_facts_document_id ON facts(document_id);
CREATE INDEX IF NOT EXISTS idx_facts_chunk_id ON facts(chunk_id);
