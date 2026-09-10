-- Migration 065: turn attestations — the Product Promise, closed.
--
-- Governor seat, work order docs/work-order-promise-step1.md (step 1 of 3).
-- One row per turn, written once from run_pipeline's outermost finally so it
-- covers all three publish terminals AND every early return. Writing it inside
-- _publish_completed instead would give every failed turn, every clarification
-- turn and every empty-payload turn no attestation at all -- which is the
-- precise failure this table exists to make impossible.
--
-- NOT named promise_kept. That name is taken by 057 / app/services/promise_kept.py
-- / SPEC_AC_V2_11: the ADJUDICATOR's per-turn quality verdict, a different
-- object. Two things called "promise" in one codebase is already one too many.
--
-- KEYED ON correlation_id, which is the turn key. chat_turns has no turn_id
-- column, and llm_calls.turn_id is NULL on all 25,310 rows in 30 days.
--
-- TWO CLOCKS, and this is the point of the table:
--   delivered_latency_s = published_at - posted_at   -- wall-clock, SPANS the
--       API process and the worker process. This is the promise clock: what
--       the user actually waited.
--   worker_latency_s    = perf_counter at publish - t0_start  -- process-local
--       and monotonic, the existing duration_ms measure. NOT replaced; joined.
--   Their difference is the QUEUE WAIT (spec section 7c segments 1+2), which
--       nothing has ever measured. It is why both columns are here.
--   A perf_counter from the API process and one from the worker cannot be
--       subtracted, which is why posted_at is wall-clock and not a counter.
--
-- WHAT THE NULLS MEAN -- each is a declared absence, never a zero:
--   promise_version/tier/posted_at NULL -> the request was enqueued before the
--       promise existed. The turn still ran and still attests. A promise is
--       never synthesised at the worker: one invented after POST is not a
--       promise, and would backfill the exact gap being measured.
--   tier NULL with a notes reason, while promise_version is SET -> a promise
--       was made but no section-7 tier applies (chat_mode 'task' has no row in
--       the tier table; or chat_mode was absent at POST, where the tier is
--       resolved later from thread state and POST cannot state one).
--   delivered_latency_s NULL -> the two wall clocks skewed and published_at
--       preceded posted_at. Deliberately NOT clamped to zero: a clamped zero
--       is indistinguishable from a very fast turn. notes carries the skew.
--   delivered_cost_c / delivered_quality NULL -> STEP 3, not yet measured.
--       A 0.0 here would be a step-1 failure reported as a cheap turn.

CREATE TABLE IF NOT EXISTS turn_attestations (
    correlation_id       TEXT        NOT NULL PRIMARY KEY,
    promise_version      TEXT,
    tier                 TEXT,
    posted_at            TIMESTAMPTZ,
    published_at         TIMESTAMPTZ NOT NULL,
    outcome              TEXT        NOT NULL,
    delivered_latency_s  DOUBLE PRECISION,
    worker_latency_s     DOUBLE PRECISION,
    delivered_cost_c     DOUBLE PRECISION,
    delivered_quality    TEXT,
    notes                TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent: run_migrations.py globs db/schema/*.sql and re-applies every
-- file on EVERY boot -- there is no applied-ledger -- so every statement in
-- this file must be safe to run repeatedly.
CREATE INDEX IF NOT EXISTS turn_attestations_published_idx
    ON turn_attestations (published_at DESC);
CREATE INDEX IF NOT EXISTS turn_attestations_outcome_idx
    ON turn_attestations (outcome, published_at DESC);
CREATE INDEX IF NOT EXISTS turn_attestations_tier_idx
    ON turn_attestations (tier, published_at DESC);
