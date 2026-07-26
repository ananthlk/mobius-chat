-- Migration 051: extend llm_calls for LLMManager (Migration A — additive + backfill).
--
-- Part of the Chat pipeline refactor. Extends the existing per-call log
-- (020_llm_calls.sql) rather than creating a parallel table, so the
-- 022 materialized view + bandit training reads are preserved (DQ-1 Option A).
--
-- Authoritative spec: mobius-chat/docs/SPEC_LLM_MANAGER.md §2.3 / §2.4.
-- DQ-1 DB gate: signed 5/5 (2026-07-25). All reviewer defects corrected here.
--
-- SEQUENCING: this is Migration A (safe to land first — additive columns +
-- backfill, no arm-history change). Migration B (052) adds the variant-aware
-- arm index + recreates the perf view and is GATED on the executed check:
--     SELECT COUNT(*) FROM llm_calls WHERE variant_id IS NULL;   -- must be 0
-- Record that 0 in the migration tracker before landing 052 (AC-18).
--
-- REQUIRES: 049_prompt_templates.sql applied first (template_id FK target).
--
-- Columns:
--   module_key     — canonical module identifier. module_key ≡ stage (1:1, DQ-2).
--   variant_id     — prompt-template variant. NOT the same axis as the existing
--                    ab_variant (legacy model-A/B marker; sparse/NULL for non-A/B
--                    calls). ab_variant is retained historical-only and is NOT an
--                    arm dimension going forward.
--   template_id    — FK to prompt_templates.id (nullable during migration).
--   turn_id        — == chat_turns.correlation_id. LLMManager.call() requires it
--                    non-null going forward (DEP-1); historical rows may be NULL.
--   is_hard_pinned — model selection was forced (config model_id non-null or a
--                    calibration_mode turn). Excluded from model-arm training.

ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS module_key     TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS variant_id     TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS template_id    INT REFERENCES prompt_templates(id);
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS turn_id        TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS is_hard_pinned BOOLEAN NOT NULL DEFAULT FALSE;
-- temperature actually used per call (Eval condition, Q3 ruling): unexplained
-- temperature variance = unexplained arm-reward variance (the bandit can't tell
-- "arm did worse" from "arm ran hotter"). CallManager writes the resolved temp.
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS temperature    REAL;

-- Backfill variant_id='default': the historical hardcoded prompt WAS the default
-- variant, so this is exact (not coarse). New/non-default variants cold-start (052).
UPDATE llm_calls SET variant_id = 'default' WHERE variant_id IS NULL;

-- Backfill module_key = stage (Eval ruling 2026-07-25). module_key ≡ stage is a
-- RENAME (exact copy), not a coarsening: for 1:1 stages this carries exact arm
-- history; for any future 1:many stage split the copied coarse label matches no
-- fine module_key and those new modules correctly cold-start. A literal copy can
-- never mis-attribute coarse reward to a finer key. Provenance of pre-refactor
-- rows is derived from created_at / NULL template_id — NOT from module_key IS NULL.
UPDATE llm_calls SET module_key = stage WHERE module_key IS NULL;
