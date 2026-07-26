-- Migration 053: prompt block persistence — blocks · versions · compositions.
--
-- Gives the v2 composable-block engine (app/services/prompt_blocks.py /
-- BlockAssembler) a persistent, versioned, governed source of truth so the
-- Composition Studio can author/version/sequence blocks per agent. Until now
-- blocks lived only in code (Block(...) literals).
--
-- Authoritative spec: mobius-chat/docs/SPEC_PROMPT_BLOCK_PERSISTENCE.md
-- Database-seat ratification: signed (2026-07-26). 8 rulings folded in below.
--
-- Additive to mig 049 (prompt_templates); NOT a replacement. Coexistence is
-- flag-gated per-module: a module resolves to an ACTIVE composition (v2 block
-- path) else falls back to its 049 template. Read-time assembly, 60s cache
-- (Q7 ruling — no compile-on-freeze). Scope GLOBAL for v1 (Q6 — org_id deferred).
--
-- Ownership split:
--   * Tables / constraints / indexes / llm_calls addendum  — LLMManager (this file).
--   * Triggers tg_authority_immutable_kind (Q3 cross-row) and
--     tg_block_body_immutable (Q4)                          — Database seat (below,
--     dropped in verbatim from Database's ratification message; do not edit).

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────
-- 1. prompt_blocks — versioned, append-only block definitions.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_blocks (
    id            SERIAL PRIMARY KEY,
    block_key     TEXT    NOT NULL,                 -- logical name, e.g. 'hipaa_context'
    version       INT     NOT NULL DEFAULT 1,       -- monotonic per block_key
    block_kind    TEXT    NOT NULL
                    CHECK (block_kind IN ('static','conditional','derived','per_turn')),
    role          TEXT    NOT NULL
                    CHECK (role IN ('system','user')),
    template_body TEXT    NOT NULL,                 -- Jinja2 source (autoescape=False)
    condition     TEXT        NULL,                 -- NULL = always-present; else a condition key
    is_authority  BOOLEAN NOT NULL DEFAULT FALSE,   -- must render after non-authority; constant per block_key (trigger, Q3)
    directives    TEXT[]  NOT NULL DEFAULT '{}',    -- Q2: TEXT[] closed domain, e.g. {'output:json'}
    owner         TEXT    NOT NULL,                 -- Q8: soft ref to fleet agent/role
    validated_at  TIMESTAMPTZ NULL,                 -- owner sign-off; authority REQUIRES non-null (CK below)
    validated_by  TEXT        NULL,                 -- Q8: soft ref
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by    TEXT    NOT NULL,                 -- Q8: soft ref

    UNIQUE (block_key, version),
    -- Q3: makes BlockAssembler.UnvalidatedAuthorityError a DB-level property (fail-closed).
    CONSTRAINT ck_authority_validated
        CHECK (is_authority = FALSE OR validated_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_prompt_blocks_key_active
    ON prompt_blocks (block_key, active);

-- ─────────────────────────────────────────────────────────────────────────
-- 2. prompt_compositions — versioned per-agent header.
--    status lifecycle: draft (editable) → validated (coherence gate passed)
--    → frozen (all members pinned + composition_hash set; reproducible).
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_compositions (
    id            SERIAL PRIMARY KEY,
    module_key    TEXT    NOT NULL,                 -- the agent/stage this runs as, e.g. 'integrator_a'
    variant_id    TEXT    NOT NULL DEFAULT 'default',
    variant_tags  JSONB   NOT NULL DEFAULT '{}',    -- Q5: 5-axis A/B tag-match lives here (per-composition only)
    version       INT     NOT NULL DEFAULT 1,       -- monotonic per (module_key, variant_id)
    status        TEXT    NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','validated','frozen')),
    active        BOOLEAN NOT NULL DEFAULT FALSE,   -- only active rows execute
    weight        REAL    NOT NULL DEFAULT 1.0,     -- A/B sampling weight within module_key
    composition_hash     TEXT        NULL,          -- FULL sha256(ordered block_key@version); set when frozen
    coherence_checked_at TIMESTAMPTZ NULL,          -- set when the coherence gate passed
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by    TEXT    NOT NULL,

    UNIQUE (module_key, variant_id, version),
    CONSTRAINT ck_validated_has_coherence
        CHECK (status = 'draft' OR coherence_checked_at IS NOT NULL),
    CONSTRAINT ck_frozen_has_hash
        CHECK (status <> 'frozen' OR composition_hash IS NOT NULL)
);

-- at most one ACTIVE composition per (module_key, variant_id)
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_composition
    ON prompt_compositions (module_key, variant_id) WHERE active;
CREATE INDEX IF NOT EXISTS idx_prompt_compositions_lookup
    ON prompt_compositions (module_key, active);
CREATE INDEX IF NOT EXISTS idx_prompt_compositions_tags
    ON prompt_compositions USING GIN (variant_tags);

-- ─────────────────────────────────────────────────────────────────────────
-- 3. prompt_composition_members — the ordered block sequence (Q1: normalized).
--    pinned_version NULL = resolve to latest active at assemble; concrete when frozen.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_composition_members (
    composition_id  INT NOT NULL REFERENCES prompt_compositions(id) ON DELETE CASCADE,
    position        INT NOT NULL,                   -- author order (authority-last applied at assemble)
    block_key       TEXT NOT NULL,
    pinned_version  INT  NULL,                      -- NULL = latest active; concrete when frozen

    PRIMARY KEY (composition_id, position),
    UNIQUE (composition_id, block_key),             -- a block appears at most once
    -- Q1: FK integrity to a specific block version (skipped when pinned_version IS NULL).
    CONSTRAINT fk_member_block
        FOREIGN KEY (block_key, pinned_version) REFERENCES prompt_blocks(block_key, version)
);

-- ─────────────────────────────────────────────────────────────────────────
-- 3.5 prompt_composition_snapshots — makes composition_hash DEREFERENCEABLE
--     (Tech Health requirement). A composition with pinned_version=NULL members
--     resolves to "latest active" at render time, so the hash is computed over a
--     transient resolution that lives nowhere else. This registry persists the
--     exact ordered (block_key, version) manifest per distinct hash, insert-on-
--     first-render (ON CONFLICT DO NOTHING), so §6's ablate/bisect/attribute
--     claims are real. Immutable audit artifact → JSONB manifest (not normalized).
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS prompt_composition_snapshots (
    composition_hash TEXT PRIMARY KEY,            -- FULL sha256 (NOT truncated — truncation risks silent mis-attribution on collision)
    manifest         JSONB NOT NULL,              -- ordered [[block_key, version], ...] AFTER filter + authority-last
    module_key       TEXT  NOT NULL,              -- denormalized for query convenience
    variant_id       TEXT  NOT NULL DEFAULT 'default',
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_composition_snapshots_module
    ON prompt_composition_snapshots (module_key, variant_id);

-- ─────────────────────────────────────────────────────────────────────────
-- 4. llm_calls addendum (spec §6) — trace every v2-path call to the exact
--    composition + block versions that produced it. composition_hash FKs to the
--    snapshot registry so a logged call's hash is ALWAYS dereferenceable to a
--    manifest (the write happens in the same txn as the llm_calls row). NULL for
--    v1-path calls (FK skipped on NULL).
-- ─────────────────────────────────────────────────────────────────────────
ALTER TABLE llm_calls
    ADD COLUMN IF NOT EXISTS composition_id   INT  NULL REFERENCES prompt_compositions(id),
    ADD COLUMN IF NOT EXISTS composition_hash TEXT NULL
        REFERENCES prompt_composition_snapshots(composition_hash);

-- ─────────────────────────────────────────────────────────────────────────
-- 5. Triggers — OWNED BY DATABASE SEAT (Q3 authority-constant-per-key,
--    Q4 immutable template_body). Database delivers the verbatim DDL in its
--    ratification message; paste here between the markers, unedited.
--
--    ⛔ LANDING GATE (Tech Health, 2026-07-26): 053 MUST NOT LAND while these say
--    [PENDING PASTE]. tg_block_body_immutable (Q4) is what makes the snapshot
--    manifest→prompt_blocks dereference trustworthy — without it, a manifest
--    resolves to whatever a (block_key, version) body is NOW, not what rendered
--    THEN, and historical ablation silently uses mutated bodies. The registry's
--    whole value rests on this trigger. Same append-only discipline as
--    compliance.hipaa_analysis_log (mig 042).
--
-- Q3: is_authority constant per block_key
CREATE OR REPLACE FUNCTION tg_authority_immutable_kind()
RETURNS TRIGGER AS $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM prompt_blocks
    WHERE block_key = NEW.block_key
      AND is_authority <> NEW.is_authority
    LIMIT 1
  ) THEN
    RAISE EXCEPTION 'is_authority cannot flip across versions of block_key %. Current: %, New: %',
      NEW.block_key,
      (SELECT is_authority FROM prompt_blocks WHERE block_key = NEW.block_key LIMIT 1),
      NEW.is_authority;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tg_authority_immutable_kind
BEFORE INSERT ON prompt_blocks
FOR EACH ROW
EXECUTE FUNCTION tg_authority_immutable_kind();

-- Q4: template_body append-only (LANDING GATE per Tech Health)
CREATE OR REPLACE FUNCTION tg_block_body_immutable()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'template_body on block_key % version % is immutable. Insert a new version instead.',
    NEW.block_key, NEW.version;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tg_block_body_immutable
BEFORE UPDATE ON prompt_blocks
FOR EACH ROW
WHEN (OLD.template_body IS DISTINCT FROM NEW.template_body)
EXECUTE FUNCTION tg_block_body_immutable();
-- ─────────────────────────────────────────────────────────────────────────

COMMIT;
