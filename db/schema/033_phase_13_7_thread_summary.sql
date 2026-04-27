-- Phase 13.7 — rolling thread summary write semantics + sidebar index.
-- This migration is informational + indexing; the column already exists
-- (see 017_chat_turns_context_summary.sql). Phase 13.7 changes WHO
-- writes to it.
--
-- Pre-Phase-13.7 behavior:
--   * insert_turn (storage/turns.py) wrote a regex-derived summary on
--     the no-thread save path
--   * _atomic_save_turn_with_messages (persistence/postgres.py) for
--     thread-with-messages saves DID NOT write context_summary, so
--     every threaded turn left the column NULL
--
-- Post-Phase-13.7 behavior:
--   * Integrator emits a top-level "thread_summary" field in its
--     AnswerCard JSON (≤60 words)
--   * run_integrate parses it onto ctx.thread_summary
--   * _atomic_save_turn_with_messages now writes it via context_summary
--   * sidebar query (get_recent_threads) joins a latest_summary CTE on
--     newest non-null context_summary per thread
--
-- Index supports the sidebar's DISTINCT ON walk: PostgreSQL needs a
-- composite (thread_id, created_at DESC) to satisfy that query without
-- a sort+filter scan. With <100k turns this isn't a hot path; we add
-- the index now so it doesn't bite once threads pile up. Conditional
-- on context_summary IS NOT NULL keeps the index narrow.
--
-- Run after 032_chat_turns_user_id.

CREATE INDEX IF NOT EXISTS idx_chat_turns_thread_summary_recent
    ON chat_turns (thread_id, created_at DESC)
    WHERE context_summary IS NOT NULL
      AND context_summary <> '';

-- Sanity: confirm the column type matches what postgres.py expects.
-- TEXT (nullable) is the contract. NOT NULL would break the
-- column-missing fallback chain — the chain assumes COALESCE works.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'chat_turns'
          AND column_name = 'context_summary'
          AND is_nullable = 'YES'
    ) THEN
        RAISE NOTICE 'WARNING: chat_turns.context_summary is not nullable. Phase 13.7 fallback chain expects nullable.';
    END IF;
END$$;
