-- migrations/026_adjudication_scores.sql
-- Rich adjudication results (v2 rubric): adjudication_scores + reporting views.
-- Does NOT replace model_performance_by_stage (022); router still uses llm_calls.quality_score.
-- Depends on: llm_calls, eval tables (optional).

CREATE TABLE IF NOT EXISTS adjudication_scores (
    score_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts               TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Linkage
    correlation_id   TEXT,
    eval_run_id      TEXT,
    test_id          TEXT,

    -- Question context
    question         TEXT,
    question_category TEXT[],       -- e.g. ["npi_lookup", "credentialing"]
    tool_fired       TEXT,
    expected_tool    TEXT,

    -- Stage metadata
    planner_model    TEXT,
    rag_model        TEXT,
    integrator_model TEXT,
    badge_model      TEXT,
    jurisdiction     TEXT,
    iterations       INTEGER,
    legacy_path      BOOLEAN,

    -- Universal dimensions
    addresses_question     NUMERIC(4,3),
    completeness           NUMERIC(4,3),
    factual_consistency    NUMERIC(4,3),
    clarity                NUMERIC(4,3),
    actionability          NUMERIC(4,3),
    escalation_quality     NUMERIC(4,3),
    language_quality       NUMERIC(4,3),
    response_efficiency    NUMERIC(4,3),
    json_compliance        NUMERIC(4,3),
    grounding              NUMERIC(4,3),
    confidence_calibration NUMERIC(4,3),

    -- Safety
    phi_boundary           NUMERIC(4,3),
    clinical_boundary      NUMERIC(4,3),

    -- Data accuracy (null = not applicable for this question)
    npi_accuracy           NUMERIC(4,3),
    org_match              NUMERIC(4,3),
    code_accuracy          NUMERIC(4,3),
    payer_accuracy         NUMERIC(4,3),
    policy_currency        NUMERIC(4,3),
    enrollment_accuracy    NUMERIC(4,3),
    roster_accuracy        NUMERIC(4,3),
    data_freshness         NUMERIC(4,3),
    source_authority       NUMERIC(4,3),
    context_accuracy       NUMERIC(4,3),
    pronoun_resolution     NUMERIC(4,3),

    -- Aggregate
    overall_score          NUMERIC(4,3),
    verdict                TEXT CHECK (verdict IN ('PASS','PARTIAL','FAIL')),
    rationale              TEXT,
    flags                  TEXT[],

    -- Attribution
    failure_stage          TEXT,
    failure_reason         TEXT,
    is_planner_fault       BOOLEAN DEFAULT FALSE,
    is_rag_fault           BOOLEAN DEFAULT FALSE,
    is_integrator_fault    BOOLEAN DEFAULT FALSE,
    is_no_fault            BOOLEAN DEFAULT FALSE,

    -- Adjudicator metadata
    adjudicator_model      TEXT,
    adjudicator_version    TEXT DEFAULT 'v2',
    used_llm               BOOLEAN DEFAULT TRUE,
    used_heuristic         BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_adj_correlation
    ON adjudication_scores (correlation_id);
CREATE INDEX IF NOT EXISTS idx_adj_eval_run
    ON adjudication_scores (eval_run_id);
CREATE INDEX IF NOT EXISTS idx_adj_test_id
    ON adjudication_scores (test_id);
CREATE INDEX IF NOT EXISTS idx_adj_verdict
    ON adjudication_scores (verdict);
CREATE INDEX IF NOT EXISTS idx_adj_ts
    ON adjudication_scores (ts DESC);
CREATE INDEX IF NOT EXISTS idx_adj_category
    ON adjudication_scores USING GIN (question_category);
CREATE INDEX IF NOT EXISTS idx_adj_flags
    ON adjudication_scores USING GIN (flags);

-- ── Views ──────────────────────────────────────────────────────────────────

-- Model quality by question category
-- Feeds model router's category-aware selection
CREATE OR REPLACE VIEW model_quality_by_category AS
SELECT
    a.integrator_model                       AS model,
    cat.category,
    COUNT(*)                                 AS scored_calls,
    AVG(a.overall_score)                     AS avg_overall,
    AVG(a.language_quality)                  AS avg_language,
    AVG(a.grounding)                         AS avg_grounding,
    AVG(a.addresses_question)                AS avg_addresses,
    AVG(a.json_compliance)                   AS avg_json,
    AVG(a.actionability)                     AS avg_actionability,
    -- Category-specific dimensions
    AVG(a.npi_accuracy)
        FILTER (WHERE a.npi_accuracy IS NOT NULL)       AS avg_npi_accuracy,
    AVG(a.payer_accuracy)
        FILTER (WHERE a.payer_accuracy IS NOT NULL)     AS avg_payer_accuracy,
    AVG(a.code_accuracy)
        FILTER (WHERE a.code_accuracy IS NOT NULL)      AS avg_code_accuracy,
    AVG(a.roster_accuracy)
        FILTER (WHERE a.roster_accuracy IS NOT NULL)    AS avg_roster_accuracy,
    AVG(a.data_freshness)
        FILTER (WHERE a.data_freshness IS NOT NULL)     AS avg_data_freshness,
    AVG(a.enrollment_accuracy)
        FILTER (WHERE a.enrollment_accuracy IS NOT NULL) AS avg_enrollment,
    AVG(a.source_authority)
        FILTER (WHERE a.source_authority IS NOT NULL)   AS avg_source_authority,
    -- Attribution
    SUM(CASE WHEN a.is_planner_fault    THEN 1 ELSE 0 END) AS planner_faults,
    SUM(CASE WHEN a.is_rag_fault        THEN 1 ELSE 0 END) AS rag_faults,
    SUM(CASE WHEN a.is_integrator_fault THEN 1 ELSE 0 END) AS integrator_faults,
    SUM(CASE WHEN a.is_no_fault         THEN 1 ELSE 0 END) AS no_fault_count,
    -- Flags
    COUNT(*) FILTER (WHERE 'JSON_BLEED' = ANY(a.flags))        AS json_bleed_count,
    COUNT(*) FILTER (WHERE 'STALE_DATA_PRESENTED' = ANY(a.flags)) AS stale_data_count,
    COUNT(*) FILTER (WHERE 'CORPUS_GAP' = ANY(a.flags))        AS corpus_gap_count
FROM adjudication_scores a
CROSS JOIN LATERAL UNNEST(a.question_category) AS cat(category)
WHERE a.ts > NOW() - INTERVAL '30 days'
  AND a.integrator_model IS NOT NULL
GROUP BY a.integrator_model, cat.category
ORDER BY cat.category, avg_overall DESC NULLS LAST;


-- Dimension heatmap — which models excel at which dimensions
CREATE OR REPLACE VIEW model_dimension_heatmap AS
SELECT
    integrator_model                         AS model,
    COUNT(*)                                 AS total_scored,
    -- Universal
    ROUND(AVG(addresses_question)::numeric, 3)    AS addresses_q,
    ROUND(AVG(completeness)::numeric, 3)          AS completeness,
    ROUND(AVG(factual_consistency)::numeric, 3)   AS factual,
    ROUND(AVG(clarity)::numeric, 3)               AS clarity,
    ROUND(AVG(language_quality)::numeric, 3)      AS language,
    ROUND(AVG(json_compliance)::numeric, 3)       AS json,
    ROUND(AVG(grounding)::numeric, 3)             AS grounding,
    ROUND(AVG(actionability)::numeric, 3)         AS actionability,
    ROUND(AVG(confidence_calibration)::numeric,3) AS confidence,
    -- Category-specific (null = not tested on this question type)
    ROUND(AVG(npi_accuracy)::numeric, 3)          AS npi_acc,
    ROUND(AVG(payer_accuracy)::numeric, 3)        AS payer_acc,
    ROUND(AVG(code_accuracy)::numeric, 3)         AS code_acc,
    ROUND(AVG(roster_accuracy)::numeric, 3)       AS roster_acc,
    ROUND(AVG(data_freshness)::numeric, 3)        AS data_fresh,
    -- Overall
    ROUND(AVG(overall_score)::numeric, 3)         AS overall
FROM adjudication_scores
WHERE ts > NOW() - INTERVAL '30 days'
  AND integrator_model IS NOT NULL
GROUP BY integrator_model
ORDER BY overall DESC NULLS LAST;


-- Attribution summary — who breaks what
CREATE OR REPLACE VIEW stage_fault_summary AS
SELECT
    COALESCE(failure_stage, 'none')          AS fault_stage,
    integrator_model,
    COUNT(*)                                 AS total_failures,
    ARRAY_AGG(DISTINCT failure_reason)
        FILTER (WHERE failure_reason IS NOT NULL) AS reasons,
    AVG(overall_score)                       AS avg_score_when_failed
FROM adjudication_scores
WHERE verdict IN ('PARTIAL', 'FAIL')
  AND ts > NOW() - INTERVAL '30 days'
GROUP BY failure_stage, integrator_model
ORDER BY total_failures DESC;


-- ── Score wiring: updated model_performance_by_stage ──────────────────────
-- Replaces the version in 022_model_performance_view.sql.
-- LEFT JOINs adjudication_scores so quality data is available
