-- Migration 049: prompt_templates — DB-backed, versioned prompt templates for LLMManager.
--
-- Part of the Chat pipeline refactor (LLMManager / PromptManager). Replaces the
-- hardcoded prompt strings in config/prompts_llm.yaml (str.format placeholders,
-- whole-file config_sha) with per-row, versioned, tag-matched, A/B-sampleable
-- Jinja2 templates.
--
-- Authoritative spec: mobius-chat/docs/SPEC_LLM_MANAGER.md §2.1 / §2.4.
-- DQ-1 DB gate: signed 5/5 (2026-07-25). This is the ratified DDL.
--
-- Scoping: GLOBAL for v1 (one template set fleet-wide) per ratified §2.4.
-- Per-org scoping is deferred; if introduced later it adds an org_id column
-- + a tenant clause in PromptManager's lookup.
--
-- Read path (PromptManager):
--   tag-match variant_tags (5 axes: voice/format/autonomy/intent/caller_mode)
--   against EngagementContext → fallback chain exact→partial→default →
--   A/B-sample active variants by weight (keyed on variant_id) →
--   Jinja2 render (autoescape=False). 60s TTL cache for hot-reload;
--   calibration_snapshot(module_key) freezes (template_id, variant_id) per batch.

CREATE TABLE IF NOT EXISTS prompt_templates (
    id            SERIAL PRIMARY KEY,
    module_key    TEXT    NOT NULL,               -- e.g. 'integrator', 'planner', 'react_1'
    role          TEXT    NOT NULL                -- system | user (an LLM call = a system + user pair)
                    CHECK (role IN ('system', 'user')),
    variant_id    TEXT    NOT NULL DEFAULT 'default',
    variant_tags  JSONB   NOT NULL DEFAULT '{}',  -- {voice, format, autonomy, intent, caller_mode}
    version       INT     NOT NULL DEFAULT 1,     -- monotonic per (module_key, role, variant_id)
    template_body TEXT    NOT NULL,               -- Jinja2 source
    active        BOOLEAN NOT NULL DEFAULT TRUE,  -- only active=true rows execute
    weight        REAL    NOT NULL DEFAULT 1.0,   -- A/B sampling weight within a module_key
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (module_key, role, variant_id, version)
);
-- role (Option A, ruled 2026-07-25): role is a fundamental property of an LLM
-- call — a module renders a system prompt + a user template. Modes (integrator
-- factual/canonical/blended) are system-role variants; the user template is a
-- separate role='user' row. Modelled as TEXT+CHECK; Database's call on TEXT vs
-- a role_type enum (routed as the 049 addendum).

-- Live-row lookup by module.
CREATE INDEX IF NOT EXISTS idx_prompt_templates_lookup
    ON prompt_templates (module_key, active);

-- GIN on variant_tags for the 5-axis tag-match JSONB lookups.
CREATE INDEX IF NOT EXISTS idx_prompt_templates_tags
    ON prompt_templates USING GIN (variant_tags);
