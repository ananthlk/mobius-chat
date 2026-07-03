-- 040 — docs_refresh_backlog view: the supply side of doc freshness.
--
-- Mirror of docs_backlog (038), for category='doc_stale': a module agent (or a
-- git hook) shipped a user-facing change and filed "this doc is now behind."
-- The weekly refresh sweep drains this view (refresh doc → re-embed →
-- store.close_signals(category='doc_stale', module=…) flips the rows to closed,
-- so they drop out here).
--
-- docs_gap  = "a user asked and we had no doc"  → docs_backlog     (demand)
-- doc_stale = "a builder changed something"     → docs_refresh_backlog (supply)
--
-- distinct_sources (not distinct_users): doc_stale rows are agent-filed, so
-- user_id carries the source (agent name / git hook id), counted here.
-- No ALTER TABLE; pure additive view over 037's columns. Safe to re-run.

CREATE OR REPLACE VIEW docs_refresh_backlog AS
SELECT
    COALESCE(m.module, '(untagged)')                         AS module,
    count(*)                                                 AS stale_hits,
    count(DISTINCT pf.user_id)                               AS distinct_sources,
    min(pf.created_at)                                       AS first_seen,
    max(pf.created_at)                                       AS last_seen,
    (array_agg(pf.verbatim ORDER BY pf.created_at DESC))[1:25] AS sample_verbatims
FROM product_feedback pf
LEFT JOIN LATERAL jsonb_array_elements_text(
    CASE WHEN pf.area_tags = '[]'::jsonb THEN NULL ELSE pf.area_tags END
) AS m(module) ON true
WHERE pf.category = 'doc_stale'
  AND pf.status <> 'closed'
GROUP BY COALESCE(m.module, '(untagged)')
ORDER BY stale_hits DESC;
