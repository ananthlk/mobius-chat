-- 039 — capability_demand view: the symmetric partner to docs_backlog.
--
-- The product-awareness loop produces two signals, not one:
--   * docs_gap        (s_top < τ_gap, corpus has nothing)      → docs_backlog
--   * feature_request (s_top ≥ τ_gap, top chunk status=planned) → HERE
-- A planned-hit is retrieval SUCCESS on a "not yet available" doc — a
-- capability-demand signal, not documentation debt. This view ranks that demand
-- by module so "which planned feature is most asked for" is answerable, the same
-- way docs_backlog answers "which doc to write next".
--
-- Also captures user-voiced feature_requests (same category) — both are "people
-- want this capability". No ALTER TABLE; pure additive view. Safe to re-run.

CREATE OR REPLACE VIEW capability_demand AS
SELECT
    COALESCE(m.module, '(untagged)')                         AS module,
    count(*)                                                 AS demand_hits,
    count(DISTINCT pf.user_id)                               AS distinct_users,
    min(pf.created_at)                                       AS first_seen,
    max(pf.created_at)                                       AS last_seen,
    (array_agg(pf.verbatim ORDER BY pf.created_at DESC))[1:25] AS sample_verbatims
FROM product_feedback pf
LEFT JOIN LATERAL jsonb_array_elements_text(
    CASE WHEN pf.area_tags = '[]'::jsonb THEN NULL ELSE pf.area_tags END
) AS m(module) ON true
WHERE pf.category = 'feature_request'
  AND pf.status <> 'closed'
GROUP BY COALESCE(m.module, '(untagged)')
ORDER BY demand_hits DESC;
