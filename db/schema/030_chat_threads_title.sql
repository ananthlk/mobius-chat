-- Phase 2.3: thread-level sidebar hygiene.
-- Adds title + turn_count so the sidebar can render distinct threads with a
-- real title instead of dumping each chat_turns.question verbatim (which was
-- showing raw URLs, ICD code lookups, and tool-invocation fragments).
-- Run after 029_instant_rag_metadata.sql.

ALTER TABLE chat_threads ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE chat_threads ADD COLUMN IF NOT EXISTS turn_count INT NOT NULL DEFAULT 0;

-- Index supports the sidebar query which sorts by updated_at and often
-- filters empty titles out.
CREATE INDEX IF NOT EXISTS idx_chat_threads_updated_title
    ON chat_threads (updated_at DESC)
    WHERE title IS NOT NULL;
