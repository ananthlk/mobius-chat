-- Migration 068: turn_rounds -- one row per round, per orchestrator.
--
-- WHY NOT turn_spans, which already holds per-module timings:
--   turn_spans carries `sampled BOOLEAN NOT NULL` and `sample_rate NUMERIC`.
--   sample_rate() reads MOBIUS_SPAN_SAMPLE_RATE and defaults to 1.0 -- so it is
--   1.0 by CONFIGURATION, not by construction. The chat seat's argument, and it
--   is the right one: an exemption is a rule that exists as agreement between a
--   table and whoever remembers to keep an env var at 1.0. Same shape as the two
--   extension writers that agreed until they didn't.
--
--   A column that can make a row ABSENT must not exist on a table that backs a
--   conformance number. Remove the ability; do not set it to 1.
--
--   And "don't invent something new" does not apply: that instruction is against
--   inventing a new SURFACE. Spans are DIAGNOSTICS that may be dropped;
--   attestations are ACCOUNTS that may not. turn_attestations (065) already
--   established that this fleet keeps the two apart. Putting a conformance
--   number in a droppable table is a category error, not reuse.
--
-- This table has NO sampling column. Its absence is the guarantee.
--
-- IDEMPOTENT: run_migrations.py:179 globs db/schema/*.sql and re-applies EVERY
-- file on EVERY boot. There is no applied-ledger.

CREATE TABLE IF NOT EXISTS turn_rounds (
    correlation_id        TEXT        NOT NULL,
    round_index           INTEGER     NOT NULL,
    orchestrator_version  TEXT        NOT NULL DEFAULT 'v1',

    -- v2's decision. NULL on rows written by a v1-only turn.
    posture               TEXT,
    directive             TEXT,
    gap_targeted          TEXT,
    rationale             TEXT,          -- the `because`; what makes a spend reviewable

    -- v1's decision, and the shadow verdict comparing them.
    v1_directive          TEXT,
    v1_reason             TEXT,
    v1_maps_to            TEXT,
    -- agree | diverge | unmapped. UNMAPPED is kept DISTINCT from DIVERGE: v1
    -- saying something the mapping does not cover is a finding about the
    -- MAPPING, not evidence against v2.
    shadow_verdict        TEXT,

    -- The round envelope: declared BEFORE, compared after. A range, never a
    -- point -- the queue wait proved a point estimate lies when the underlying
    -- is bimodal (13 warm @ 0.023s, 3 cold @ 3.76s, nothing between).
    declared_latency_lo_s DOUBLE PRECISION,
    declared_latency_hi_s DOUBLE PRECISION,
    declared_cost_lo_c    DOUBLE PRECISION,
    declared_cost_hi_c    DOUBLE PRECISION,
    delivered_latency_s   DOUBLE PRECISION,
    delivered_cost_c      DOUBLE PRECISION,

    gaps_opened           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    gaps_closed           JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- Tool Manifest: turns DECLARED latencies into MEASURED ones via set-level
    -- co-occurrence, without waiting for per-tool spans nobody owns.
    -- declared_version pins which declaration was in force, so an owner editing
    -- a number cannot retroactively rewrite the accuracy history that was about
    -- to correct it.
    tools_offered         JSONB,
    declared_latency_ms   DOUBLE PRECISION,
    declared_version      TEXT,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (correlation_id, round_index, orchestrator_version)
);

CREATE INDEX IF NOT EXISTS turn_rounds_cid_idx
    ON turn_rounds (correlation_id, round_index);

-- The R0 review query: every divergence, newest first.
CREATE INDEX IF NOT EXISTS turn_rounds_verdict_idx
    ON turn_rounds (shadow_verdict, created_at DESC)
    WHERE shadow_verdict IS NOT NULL;

-- ── the overrun, added 2026-09-11 ────────────────────────────────────────────
--
-- Ananth: "would you relax a constraint like cost or latency when you are this
-- close to a final answer?" Yes -- into the BAND, on evidence of convergence,
-- bounded, and on the record.
--
-- This column is the record. A promise breach and a deliberate evidenced
-- overrun look IDENTICAL in a latency number and are opposite facts about the
-- governor: one is a miss, the other is a judgement it was authorised to make
-- and said so. Without the flag, every conformance report conflates them and
-- the governor gets blamed for exercising a budget it was given.
ALTER TABLE turn_rounds ADD COLUMN IF NOT EXISTS overran BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS turn_rounds_overran_idx
    ON turn_rounds (created_at DESC) WHERE overran;
