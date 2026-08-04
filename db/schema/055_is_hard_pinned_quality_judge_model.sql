-- Migration 055: is_hard_pinned + quality_judge_model columns on llm_calls.
--
-- Part of the Bandit Agent reward-contract work (steps 1-2). Additive only --
-- the matview FILTER/DROP CASCADE recreation is a SEPARATE follow-up migration,
-- pending two open design questions flagged to Database/Eval:
--   1. How should is_hard_pinned=NULL (every pre-migration row) be treated in
--      the avg_quality/quality_samples/quality_stddev FILTER -- counted as
--      "not pinned" (IS NOT TRUE) or excluded like a known pin (= false only)?
--      The latter means every stage's quality_samples visibly drops on deploy
--      day until new data accumulates -- a real, visible behavior change that
--      shouldn't be picked silently in a migration file.
--   2. Where does the quality_judge_model FILTER (excluding flash-graded RAG
--      rows until rag_fact_check locks to pro) actually apply -- inside
--      model_performance_by_stage itself (affects every stage's avg_quality,
--      not just RAG), or a separate RAG-producer-specific aggregate?
--
-- This migration only adds the columns and is safe to apply independently --
-- both new columns are NULL for all existing rows and NULL-tolerant on read
-- (no code path assumes NOT NULL yet).

ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS is_hard_pinned BOOLEAN;
COMMENT ON COLUMN llm_calls.is_hard_pinned IS
    'true when router.select() returned via a profile pin (router_meta["mode"] not in ("thompson","exploration")) rather than Thompson sampling -- lets reward aggregates exclude forced-model turns from quality/latency learning. NULL = pre-migration row, pin status unknown.';

ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS quality_judge_model TEXT;
COMMENT ON COLUMN llm_calls.quality_judge_model IS
    'The adjudicator LLM model that produced this row''s quality_score (e.g. "gemini-2.5-pro"). Lets reward aggregates filter to a single trusted judge until model-specific grading variance is calibrated out (Eval, 2026-08-04: flash grades RAG-producer arms materially differently than pro).';
