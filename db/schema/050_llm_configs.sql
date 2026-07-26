-- Migration 050: llm_configs — per-module LLM invocation config for LLMManager.
--
-- Part of the Chat pipeline refactor (LLMManager / ConfigManager). Centralizes
-- the per-module temperature/max_tokens/top_p/stop/timeout/fallback that today
-- are scattered per call site + config/prompts_llm.yaml:llm.
--
-- Authoritative spec: mobius-chat/docs/SPEC_LLM_MANAGER.md §2.2 / §2.4.
-- DQ-1 DB gate: signed 5/5 (2026-07-25). This is the ratified DDL.
--
-- Scoping: GLOBAL for v1 per ratified §2.4 (per-org deferred; see 049 header).
--
-- Model ownership (DQ-4): model_id is NULLABLE.
--   NULL      → the Thompson-sampling bandit (model_registry) chooses the model.
--   non-NULL  → hard-pin escape hatch (equivalent to a model_profiles.yaml pin).
--               A hard-pinned turn sets llm_calls.is_hard_pinned=TRUE and is
--               EXCLUDED from model-arm bandit training (selection bias), but
--               remains eligible for prompt-variant reward. ConfigManager owns
--               temperature/max_tokens/top_p/stop/timeout/fallback and never
--               competes on model selection.

CREATE TABLE IF NOT EXISTS llm_configs (
    module_key     TEXT PRIMARY KEY,
    model_id       TEXT,                          -- NULL = bandit chooses; non-NULL = hard-pin
    temperature    REAL NOT NULL DEFAULT 0.1,
    max_tokens     INT  NOT NULL DEFAULT 1000,
    top_p          REAL,
    stop_sequences TEXT[],
    timeout_ms     INT  NOT NULL DEFAULT 30000,
    fallback_model TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
