-- Migration 067: thread_gaps — the gap ledger, persisted across turns.
--
-- Ananth, 2026-09-11: "the single biggest thing we have across turns is the gap
-- list. I don't think we persist that. We should create a new table to persist
-- it and load it at state_load. It tells react everything we want to do."
-- And: the ledger is THREAD-scoped, not turn-scoped.
--
-- WHY A TABLE AND NOT A KEY IN chat_state.state_json (which is already loaded at
-- state_load, so a key there would be free to read):
--
--   1. The CAPABILITY build queue needs CROSS-THREAD aggregation. "Which gaps
--      exit CAPABILITY across many threads and users" names sources we do not
--      have, in users' own words. That query is impossible efficiently inside a
--      per-thread JSONB blob and trivial against an indexed table.
--   2. chat_state carries a documented compare-and-set hazard (storage/threads.py
--      :750): a turn writes chat_state more than once, so a version captured at
--      read time is stale by the second write. Gaps are written on a different
--      cadence and should not inherit that contention.
--   3. A gap has structure worth constraining. JSONB accepts a gap with no id.
--
-- WHY gap_id IS GOVERNOR-MINTED AND NOT MODEL-GENERATED: a model cannot be relied
-- on to remember an identifier across rounds, and every round is a fresh call.
-- The governor holds the ledger, injects the open list into the prompt each
-- round, and react answers BY REFERENCE (closed: [G3], new: [...]). React does
-- zero bookkeeping and identity is exact rather than inferred by matching text.
--
-- WHY opened_promise_version IS STORED ON THE ROW: a gap outlives the promise it
-- was opened under. "We could not close this in 13s" and "we could not close
-- this in 95s" are different facts about the same gap. Storing the version by
-- reference to a file someone can edit would let one edit retroactively change
-- the meaning of every historical row -- the same defect found in
-- turn_attestations on 2026-09-10, fixed the same way: the value travels with
-- the row.
--
-- WHY attempted_by IS NOT OPTIONAL: it is the ONLY thing that separates
-- "unsuccessful because of budget" (attempts were cut off -> offer to continue)
-- from "unsuccessful because of lack of tools" (every attempt ran and returned
-- nothing -> do NOT offer, retrying fails identically). Without it the system
-- defaults to the flattering explanation, because "I ran out of time" sounds
-- better than "we cannot answer this".
--
-- IDEMPOTENT: app/db/run_migrations.py:179 globs db/schema/*.sql and re-applies
-- EVERY file on EVERY boot. There is no applied-ledger. Every statement here
-- must be safe to run repeatedly.

CREATE TABLE IF NOT EXISTS thread_gaps (
    gap_id                  TEXT        NOT NULL PRIMARY KEY,
    thread_id               UUID        NOT NULL,

    -- react's own words. NEVER rewritten -- rewriting loses the user's framing,
    -- and the text is what a CAPABILITY exit reports to product.
    text                    TEXT        NOT NULL,

    status                  TEXT        NOT NULL DEFAULT 'open',
    importance              TEXT,

    opened_turn             TEXT        NOT NULL,
    opened_round            INTEGER,
    opened_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    opened_promise_version  TEXT,

    -- [{lever, tool, model, round, outcome}] -- what has already been spent on
    -- this gap. Prevents buying the same round twice, and decides the exit mode.
    attempted_by            JSONB       NOT NULL DEFAULT '[]'::jsonb,

    closed_turn             TEXT,
    closed_round            INTEGER,
    closed_at               TIMESTAMPTZ,

    -- complete | budget | capability -- set when status leaves 'open'.
    -- 'capability' rows aggregated across threads ARE the build queue.
    exit_mode_at_close      TEXT,

    reopened_count          INTEGER     NOT NULL DEFAULT 0,
    last_seen_turn          TEXT,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The state_load read: open gaps for one thread. state_load is currently
-- 412.7ms p50 (down from 1572.9ms) -- this read must be measured after it lands,
-- not assumed, and it belongs beside the existing state read rather than after it.
CREATE INDEX IF NOT EXISTS thread_gaps_thread_status_idx
    ON thread_gaps (thread_id, status);

-- The build queue: gaps that exited for lack of a source, newest first.
CREATE INDEX IF NOT EXISTS thread_gaps_exit_mode_idx
    ON thread_gaps (exit_mode_at_close, closed_at DESC)
    WHERE exit_mode_at_close IS NOT NULL;

-- Staleness sweeps and thread-level conformance.
CREATE INDEX IF NOT EXISTS thread_gaps_opened_at_idx
    ON thread_gaps (opened_at DESC);

-- status is a small closed set; a legal value with zero rows is a signal we want
-- to be able to read, so the constraint is named and additive.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'thread_gaps_status_chk'
    ) THEN
        ALTER TABLE thread_gaps
            ADD CONSTRAINT thread_gaps_status_chk
            CHECK (status IN ('open', 'closed', 'abandoned'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'thread_gaps_exit_mode_chk'
    ) THEN
        ALTER TABLE thread_gaps
            ADD CONSTRAINT thread_gaps_exit_mode_chk
            CHECK (exit_mode_at_close IS NULL
                   OR exit_mode_at_close IN ('complete', 'budget', 'capability'));
    END IF;
END $$;
