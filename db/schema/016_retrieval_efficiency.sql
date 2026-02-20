-- Retrieval efficiency tables for data science. Same DB as chat_turns (CHAT_RAG_DATABASE_URL).
-- Captures full trace: extract, merge, rerank, decay, blend, and scoring components (why high/low).

-- One row per retrieval invocation (per subquestion)
CREATE TABLE IF NOT EXISTS retrieval_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id TEXT NOT NULL,
    subquestion_id TEXT,
    subquestion_text TEXT,
    path TEXT,
    n_factual INT,
    n_hierarchical INT,
    bm25_raw_n INT,
    vector_raw_n INT,
    vector_filtered_n INT,
    merged_n INT,
    n_added_bm25 INT,
    n_skipped_bm25 INT,
    n_added_vector INT,
    n_skipped_vector INT,
    merged_ids_by_source JSONB,
    n_chunks_rerank_input INT,
    n_chunks_after_decay INT,
    by_category_keys JSONB,
    decay_per_category JSONB,
    blend_chunks_input_n INT,
    blend_n_sentence_pool INT,
    blend_n_paragraph_pool INT,
    blend_n_output INT,
    n_assembled INT,
    n_corpus INT,
    n_google INT,
    reranker_config_snapshot JSONB,
    bm25_sigmoid_snapshot JSONB,
    raw_by_signal JSONB,
    norm_by_signal JSONB,
    extract_ms INT,
    merge_ms INT,
    rerank_ms INT,
    assemble_ms INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_retrieval_runs_correlation_id ON retrieval_runs(correlation_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_runs_created_at ON retrieval_runs(created_at DESC);

-- One row per chunk that reached assembly, with full scoring breakdown
CREATE TABLE IF NOT EXISTS retrieval_docs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    retrieval_run_id UUID NOT NULL REFERENCES retrieval_runs(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    chunk_id TEXT,
    document_id TEXT,
    document_name TEXT,
    page_number INT,
    retrieval_source TEXT,
    provision_type TEXT,
    bm25_raw_score FLOAT,
    bm25_sigmoid_k FLOAT,
    bm25_sigmoid_x0 FLOAT,
    similarity FLOAT,
    rerank_score FLOAT,
    reranker_signals JSONB,
    confidence_label TEXT,
    text_preview TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_retrieval_docs_run_id ON retrieval_docs(retrieval_run_id);
