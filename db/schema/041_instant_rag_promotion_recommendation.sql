-- Migration 041: instant_rag_uploads — promotion recommendation + PHI classification
--
-- Adds the content-eligibility layer for the 3-tier visibility model
-- (private -> org -> public). When an upload finishes indexing, an async
-- pass classifies the doc and recommends the SAFE ceiling; the user
-- approves/overrides. Promotion is gated: confirmed_visibility may not
-- exceed suggested_visibility without an explicit override (+ role for public).
--
-- Populated by the PHI classifier (mobius-skills/phi-classifier) /classify
-- response, called async from chat's post-ready path. See
-- docs/instant-rag-vault-proposal.md §2.5.
--
-- Safety: conservative default. Unclassified OR degraded-mode rows are
-- treated as private until (re)classified. classifier_version + layers_run
-- let us find + re-classify rows when the recall bar improves or when a
-- verdict was produced with the LLM layer degraded/timed-out.

ALTER TABLE instant_rag_uploads
    -- The recommended safe ceiling (system) and the user's approved choice.
    ADD COLUMN IF NOT EXISTS suggested_visibility  TEXT
        CHECK (suggested_visibility  IN ('private','org','public')),
    ADD COLUMN IF NOT EXISTS confirmed_visibility  TEXT
        CHECK (confirmed_visibility  IN ('private','org','public')),

    -- PHI classifier verdict.
    ADD COLUMN IF NOT EXISTS phi_flag              BOOLEAN,
    ADD COLUMN IF NOT EXISTS phi_evidence          JSONB,     -- [{category, redacted_span, offset}] — MASKED only, never raw PHI
    ADD COLUMN IF NOT EXISTS identifiers_found     JSONB,     -- [category strings]
    ADD COLUMN IF NOT EXISTS classifier_confidence REAL,      -- 0..1

    -- Traceability for re-classification.
    ADD COLUMN IF NOT EXISTS classifier_version    TEXT,      -- detector version; bump => candidate for re-classify
    ADD COLUMN IF NOT EXISTS layers_run            JSONB,     -- [regex|ner|llm] actually executed; missing 'llm' => degraded verdict
    ADD COLUMN IF NOT EXISTS classified_at         TIMESTAMPTZ;

-- Find rows that still need (re)classification:
--   - never classified (classified_at IS NULL), or
--   - classified in degraded mode (llm layer absent) — re-run when LLM healthy.
-- Kept as a plain index on classified_at; degraded rows are filtered in-query
-- via layers_run so the index stays small and general.
CREATE INDEX IF NOT EXISTS idx_instant_rag_uploads_classified_at
    ON instant_rag_uploads (classified_at);

COMMENT ON COLUMN instant_rag_uploads.suggested_visibility IS
    'System-recommended SAFE ceiling (private|org|public). PHI/confidential => private. Conservative default: null/unclassified treated as private.';
COMMENT ON COLUMN instant_rag_uploads.confirmed_visibility IS
    'User-approved visibility. Gated: may not exceed suggested_visibility without explicit override (+ role for public).';
COMMENT ON COLUMN instant_rag_uploads.phi_evidence IS
    'MASKED evidence only ([{category, redacted_span, offset}]). Never store raw PHI values.';
COMMENT ON COLUMN instant_rag_uploads.layers_run IS
    'Classifier layers executed ([regex,ner,llm]). Missing llm => degraded verdict (defaulted private); re-classify when LLM healthy.';
