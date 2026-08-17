-- fetch_document candidate-lookup performance (2026-08-17).
--
-- published_rag_metadata is chunk-grain (~1.9M rows). fetch_document
-- resolves documents by name via ILIKE '%token%' over
-- display_name/filename/payer. A leading-wildcard ILIKE can't use a btree
-- index, so this was a full parallel seq scan — measured at 19-25 SECONDS
-- per query, which is the "Searching document index…" stall that blew past
-- the chat turn's 90s deadline.
--
-- A pg_trgm GIN index makes ILIKE '%…%' index-assisted (for tokens ≥ 3
-- chars; the skill only sends ≥3-char tokens to the filter for exactly this
-- reason). Measured after: "FL.UM.87" 25s → 174ms, "sunshine provider
-- manual" 19s → ~700ms. Also benefits any other consumer that name-matches
-- this table.
--
-- IF NOT EXISTS on both so this is a safe no-op after the first apply (the
-- GIN build over 1.9M rows takes ~35s the first time only).

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_prm_trgm_names
    ON published_rag_metadata
    USING gin (
        document_display_name gin_trgm_ops,
        document_filename      gin_trgm_ops,
        document_payer         gin_trgm_ops
    );
