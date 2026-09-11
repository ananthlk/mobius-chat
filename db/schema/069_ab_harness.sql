-- Migration 069: the A/B harness -- runs, per-arm turns, and human verdicts.
--
-- Ananth, 2026-09-11: "I don't imagine this as a one-time thing but as a setup
-- that we can capitalize on."
--
-- WHY THIS IS INFRASTRUCTURE AND NOT A PAGE: this fleet has ZERO golden
-- fixtures, recall has no measure, tool-exposure AFFECTS.quality is UNMEASURED,
-- and the groundedness floor never runs on agentic. A person reading two
-- renderings side by side is the ONLY quality signal that exists -- not a
-- fallback for when a metric is unavailable, but the absence of any metric for
-- the term the promise calls quality.
--
-- And every comparison a human judges yields a (question, better answer,
-- reason) triple -- a golden fixture. The harness generates its own missing
-- input. Nothing else here does.
--
-- PRODUCTION ROUTES; THE HARNESS FORKS. A live turn goes to exactly one
-- orchestrator -- two writers sharing state is the defect removed four times
-- this week. The harness deliberately runs BOTH arms on identical input because
-- nobody is being served twice. Nothing in this file is a precedent for forking
-- a live turn.
--
-- IDEMPOTENT: run_migrations.py globs db/schema/*.sql and re-applies every file
-- on every boot.

-- One row per experiment. A run is a RECORD, not a one-off: without this, every
-- comparison is unreproducible the day after it is looked at.
CREATE TABLE IF NOT EXISTS ab_runs (
    run_id          TEXT        NOT NULL PRIMARY KEY,
    set_id          TEXT        NOT NULL,

    arm_a_id        TEXT        NOT NULL,
    arm_a_label     TEXT        NOT NULL,
    arm_b_id        TEXT        NOT NULL,
    arm_b_label     TEXT        NOT NULL,

    -- Rule 5, enforced by the schema rather than by discipline. A comparison
    -- without these stated is a shape with no experiment behind it -- two
    -- claims were withdrawn in this program on 2026-09-10 for exactly that
    -- omission (a tier table that held the question constant; a tokenizer claim
    -- that held content type constant).
    held_constant   JSONB       NOT NULL DEFAULT '[]'::jsonb,
    varied          JSONB       NOT NULL DEFAULT '[]'::jsonb,

    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT        NOT NULL DEFAULT 'open'
);

-- One row per (run, question, arm) -> the turn that arm produced.
-- Two arms on one question means TWO correlation_ids; nothing else in the
-- system links them, and this is that join key.
CREATE TABLE IF NOT EXISTS ab_run_questions (
    run_id          TEXT        NOT NULL,
    question_id     TEXT        NOT NULL,
    arm_id          TEXT        NOT NULL,
    correlation_id  TEXT,
    thread_id       UUID,           -- a FRESH thread per arm: same thread would
                                    -- make the second arm a follow-up, not a
                                    -- comparison
    status          TEXT        NOT NULL DEFAULT 'pending',
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, question_id, arm_id)
);

CREATE INDEX IF NOT EXISTS ab_run_questions_cid_idx
    ON ab_run_questions (correlation_id) WHERE correlation_id IS NOT NULL;

-- The human's judgement. ONE row per (run, question).
--
-- 🔴 NOTHING MAY AGGREGATE THIS. No COUNT, no rate, no "v2 better: 13/20".
-- The moment a score exists it is quoted as an exit criterion, and the
-- distinction between "zero data points for the criteria, twenty for
-- judgement" stops being observed. A fixture is evidence; a tally is a claim.
-- `notes` is the load-bearing column -- WHY it was better is the fixture;
-- WHICH was better is just a label.
CREATE TABLE IF NOT EXISTS ab_verdicts (
    run_id          TEXT        NOT NULL,
    question_id     TEXT        NOT NULL,
    better          TEXT,           -- arm id, or 'tie'
    notes           TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, question_id)
);
