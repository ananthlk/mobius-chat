-- Doc-grain name-match acceleration (2026-08-17).
--
-- published_rag_metadata is chunk-grain (~1.95M rows, ~9210 documents).
-- fetch_document's name-match tier (_fetch_candidates) runs
-- ILIKE ANY(patterns) over display_name/filename/payer. For a document
-- title made of COMMON tokens — e.g.
-- "59G-4.150 Inpatient Hospital Services Coverage Policy Final" — the
-- trigram index on the chunk table still matches ~130k chunk rows (token
-- "150" alone hits 146k filenames), and the DISTINCT ON dedup down to
-- documents reads all those heap rows. Measured: 7,302 ms for that title.
-- The ">= 3-char token" filter (058) only helps when tokens are RARE
-- (FL.UM.87 -> 174ms); it does nothing for common-word titles, which is
-- the normal case for real documents.
--
-- This materialized view collapses the table to ONE row per document
-- (~9210 rows). The identical ILIKE ANY over 9k rows is ~220 ms — a ~30x
-- win — and stays fast regardless of token selectivity. Refreshed
-- CONCURRENTLY (~6s, non-blocking) on a ~10-min cadence by
-- app.services.docgrain_refresh. fetch_document falls back to the base
-- table on a MV miss, so a doc published within the last refresh window
-- still resolves (it just pays the slow path that one time).
--
-- IF NOT EXISTS throughout: first apply builds the MV (~10s, one-time);
-- every later startup is a no-op.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE MATERIALIZED VIEW IF NOT EXISTS published_rag_documents AS
SELECT DISTINCT ON (document_id)
    document_id,
    document_display_name,
    document_filename,
    document_payer,
    document_state,
    document_program,
    document_authority_level,
    updated_at
FROM published_rag_metadata
WHERE document_id IS NOT NULL
ORDER BY document_id, updated_at DESC;

-- REQUIRED for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS idx_prd_document_id
    ON published_rag_documents (document_id);

-- Serves the ILIKE ANY name filter (planner may still prefer a seq scan
-- at 9k rows — either way it's sub-300ms).
CREATE INDEX IF NOT EXISTS idx_prd_trgm_names
    ON published_rag_documents
    USING gin (
        document_display_name gin_trgm_ops,
        document_filename      gin_trgm_ops,
        document_payer         gin_trgm_ops
    );
