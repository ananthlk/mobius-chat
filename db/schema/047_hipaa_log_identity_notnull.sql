-- Migration 047: hipaa_analysis_log — enforce the solid trace (NOT NULL)
--
-- The final step of the 2026-07-19 HIPAA solid-trace ruling, applied ONLY
-- after BOTH writers proved compliant (constraint-after-compliance):
--   * chat gate writer (6db2a19): verified by executing its exact 19-column
--     INSERT shape against the live table.
--   * RAG org-docs writer (d2073e7 / rev 00436): verified by acceptance test —
--     a real ingest produced org_slug='phi-gate-test-clinic' (canonical, via
--     reverse lookup in org_docs_namespaces) + org_source='gate'.
-- Green light relayed by the PHI/HIPAA agent conditional on that observable.
--
-- user_id and org_slug can now never be NULL: unresolved identity uses the
-- reserved '__unresolved__' token (ruling B), never NULL, never a service
-- name. The trace is enforced by the table, not by writer discipline.

ALTER TABLE compliance.hipaa_analysis_log
    ALTER COLUMN user_id  SET NOT NULL,
    ALTER COLUMN org_slug SET NOT NULL;
