-- 038 — docs_backlog view: the product-awareness curation backlog.
--
-- Ranks documentation gaps (product_feedback.category='docs_gap', filed by the
-- product-awareness skill when product_help_search misses) by module. Per the
-- integration contract (docs/product-awareness-feedback-contract.md): ONE ROW
-- PER MISS with the full verbatim preserved — the verbatims are the asset (doc
-- source material + eval query bank). No count column, no dedup at write time;
-- frequency is a ranking concern, computed here.
--
-- No ALTER TABLE — pure additive view over 037's columns. Safe to re-run.
--
-- Full verbatims for a module (for doc-writing / eval bank), not just the sample:
--   SELECT verbatim, created_at FROM product_feedback
--   WHERE category='docs_gap' AND area_tags ? 'chat' ORDER BY created_at DESC;
-- (the `?` operator tests JSONB-array membership of the slug.)

CREATE OR REPLACE VIEW docs_backlog AS
SELECT
    COALESCE(m.module, '(untagged)')                         AS module,
    count(*)                                                 AS gap_hits,
    count(DISTINCT pf.user_id)                               AS distinct_users,
    min(pf.created_at)                                       AS first_seen,
    max(pf.created_at)                                       AS last_seen,
    -- a bounded sample for eyeballing; full set via the raw query above
    (array_agg(pf.verbatim ORDER BY pf.created_at DESC))[1:25] AS sample_verbatims
FROM product_feedback pf
LEFT JOIN LATERAL jsonb_array_elements_text(
    CASE WHEN pf.area_tags = '[]'::jsonb THEN NULL ELSE pf.area_tags END
) AS m(module) ON true
WHERE pf.category = 'docs_gap'
  AND pf.status <> 'closed'
GROUP BY COALESCE(m.module, '(untagged)')
ORDER BY gap_hits DESC;
