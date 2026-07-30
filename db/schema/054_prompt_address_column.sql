-- Migration 054: prompt_address — decouple composition lookup from module_key.
--
-- Ruled by Chat Architecture (2026-07-27) for the ReAct v2 block decomposition:
-- module_key MUST stay the bandit arm identifier (react_N, per round-number,
-- run-length-variable — see project_llm_manager_v2 Q3/E3 decoupling). ReAct's
-- compositions are phase-addressed (react_explore/react_synthesize/react_draft),
-- which does NOT vary 1:1 with the arm. prompt_address is that second, decoupled
-- lookup key. Existing integrator compositions are UNCHANGED (prompt_address
-- stays NULL; module_key continues to double as their lookup key).
--
-- Additive to 053. No data migration — nullable column, existing rows untouched.

BEGIN;

ALTER TABLE prompt_compositions
    ADD COLUMN IF NOT EXISTS prompt_address TEXT NULL;

COMMENT ON COLUMN prompt_compositions.prompt_address IS
    'Optional composition lookup key, decoupled from module_key (the bandit arm). '
    'NULL for existing integrator compositions (module_key doubles as the lookup key). '
    'Set for phase-addressed compositions (e.g. react_explore) whose module_key/arm '
    'varies independently (e.g. react_N per round-number) from which composition to run.';

-- One ACTIVE composition per (prompt_address, variant_id) when prompt_address is used —
-- mirrors uq_active_composition's module_key guarantee, for the new key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_composition_by_address
    ON prompt_compositions (prompt_address, variant_id) WHERE active AND prompt_address IS NOT NULL;

-- No version-uniqueness constraint added on (prompt_address, variant_id, version):
-- the existing (module_key, variant_id, version) UNIQUE already prevents duplicate
-- version rows regardless of prompt_address, since module_key remains NOT NULL and
-- populated on every row (react's rows still set a real module_key/arm value).

CREATE INDEX IF NOT EXISTS idx_prompt_compositions_address
    ON prompt_compositions (prompt_address, active) WHERE prompt_address IS NOT NULL;

COMMIT;
