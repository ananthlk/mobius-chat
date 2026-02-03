-- Per-source feedback: thumbs up/down for each source in a turn.
-- Run against the same DB as chat_feedback (CHAT_RAG_DATABASE_URL).
-- source_index is 1-based index in the turn's sources array.

CREATE TABLE IF NOT EXISTS chat_source_feedback (
    correlation_id TEXT NOT NULL,
    source_index INT NOT NULL,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT (now()),
    PRIMARY KEY (correlation_id, source_index)
);

CREATE INDEX IF NOT EXISTS idx_chat_source_feedback_correlation ON chat_source_feedback(correlation_id);
