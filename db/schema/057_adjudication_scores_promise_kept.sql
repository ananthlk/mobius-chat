-- Migration 057: promise-KEPT verdict columns on adjudication_scores.
--
-- AC-v2-11 (docs/SPEC_AC_V2_11_PROMISE_KEPT.md, §7 — Eval-Architect).
-- adjudication_scores (026) is the per-turn verdict row; grade_promise_kept
-- (app/services/promise_kept.py) runs right after the v2 adjudicator inside
-- post_run_adjudication.py, alongside the existing sub_scores it already
-- writes there, so this is the natural home for the promise-kept verdict.
--
-- Additive only, no backfill needed (no promise_kept data exists before
-- this ships — insert_adjudication_score_row simply passes NULLs for
-- historical code paths that don't populate these fields yet).

ALTER TABLE adjudication_scores ADD COLUMN IF NOT EXISTS promise_kept_overall TEXT;
ALTER TABLE adjudication_scores ADD COLUMN IF NOT EXISTS promise_kept_scores  JSONB;
ALTER TABLE adjudication_scores ADD COLUMN IF NOT EXISTS promise_ruler        TEXT;
