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

-- ── what the round actually ran, added 2026-09-11 ────────────────────────────
--
-- tools_offered was specified and never written -- a column decided and
-- discarded, the third such field in one night.
--
-- tool_called is the half that unblocks the latency question. Measured per-tool
-- round durations span 10x (healthcare_query 23.0s p50 .. service_line_limits
-- 2.2s), so "what did this round run" is the join that turns Tool Manifest's
-- DECLARED latencies into MEASURED ones -- without waiting for per-tool spans
-- nobody owns.
--
-- tools_offered records what was AVAILABLE to the model that round. On v1 rows
-- that is the whole manifest, recorded as a marker rather than 57 names: the
-- point of the column is to compare an offer against what was used, and v1's
-- offer is "everything", which is exactly the baseline being replaced.
ALTER TABLE turn_rounds ADD COLUMN IF NOT EXISTS tool_called TEXT;

CREATE INDEX IF NOT EXISTS turn_rounds_tool_called_idx
    ON turn_rounds (tool_called) WHERE tool_called IS NOT NULL;

-- ── 2026-09-11: the round clock, and two columns that never had a writer ────
-- 100 rows in, `delivered_latency_s` and `delivered_cost_c` were populated on
-- ZERO of them. I declared them here and never wired them -- the same
-- consumer-with-no-producer defect this program has filed against three other
-- seats this week, in my own table. An unwritten column is worse than an
-- absent one: it reads as a legitimate negative.
--
-- They are dropped rather than wired, because `delivered_latency_s` was also
-- the WRONG SHAPE. The only per-round clock available is `elapsed_s`, stamped
-- at round START -- storing a start time in a column named for a delivered
-- duration is precisely the naming that produced the off-by-one I had to
-- withdraw a per-tool cost claim over on 2026-09-11.
--
-- `round_duration_s` is the successor difference, computed at settle where the
-- successor is known: duration(n) = elapsed(n+1) - elapsed(n). The LAST round
-- is NULL, not the remainder: the final round's span includes publish, and a
-- number that silently includes publish would be filed as a tool latency.
ALTER TABLE turn_rounds ADD COLUMN IF NOT EXISTS round_duration_s DOUBLE PRECISION;
ALTER TABLE turn_rounds DROP COLUMN IF EXISTS delivered_latency_s;
ALTER TABLE turn_rounds DROP COLUMN IF EXISTS delivered_cost_c;

-- ── STEP 2: what v2 ACTUALLY RAN, vs what it would have chosen ─────────────
-- `directive` above is the posture machine's own vocabulary (DISCOVER /
-- CLOSE) and is NULL for NARROW and COMMUNICATE. It is NOT the thing the loop
-- executed. Without these three, a v2 turn's row records the arm and the
-- posture and says nothing about what the substitution did -- decided and
-- discarded, the third time in this program and the second in my own module.
--
--   applied_directive  v1's vocabulary: extend | finalize | complete. THE
--                      substitution. NULL on a v1 turn, which is a real
--                      distinction: v1 turns have no applied v2 directive.
--   v2_applied         TRUE only if the substitution actually took effect. A
--                      v2 turn whose hook raised has already fallen back to
--                      v1's directive; counting it as v2 would file it in the
--                      wrong population.
--   prompt_mismatch    the KNOWN-WRONG prompt, declared per round: EXPLORE and
--                      ALTERNATIVES receive v1's remediation prompt because v1
--                      has no other extend prompt at this branch. Carried so
--                      the comparison can separate "v2 chose badly" from "v2
--                      chose well and got the wrong prompt for it".
ALTER TABLE turn_rounds ADD COLUMN IF NOT EXISTS applied_directive TEXT;
ALTER TABLE turn_rounds ADD COLUMN IF NOT EXISTS v2_applied        BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE turn_rounds ADD COLUMN IF NOT EXISTS prompt_mismatch   TEXT;
