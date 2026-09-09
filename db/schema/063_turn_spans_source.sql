-- Migration 063: turn source (real / smoke / eval).
--
-- Fleet percentiles were correct arithmetic over the WRONG POPULATION: the
-- deploy smoke test's turns hit cold connection pools at ~400-500ms per DB
-- call while real turns run at ~30-39ms, and averaging the two produced a
-- p50 that described neither. Fixing the maths without fixing the sampling
-- frame is a subtler version of the same error.
--
-- Tagging the source rather than hard-excluding smoke cids also lines up
-- with aligning telemetry to the QA audit runs: once a turn carries a
-- source, "real traffic only" and "audit runs only" are the same mechanism
-- with a different filter value, not two features.
ALTER TABLE turn_spans ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'real';

CREATE INDEX IF NOT EXISTS turn_spans_source_idx ON turn_spans (source, created_at DESC);
