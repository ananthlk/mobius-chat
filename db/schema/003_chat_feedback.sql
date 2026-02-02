-- Chat feedback: one row per turn (correlation_id). Thumbs up/down + optional comment.
-- Run against the same DB as published_rag_metadata (CHAT_RAG_DATABASE_URL).

CREATE TABLE IF NOT EXISTS chat_feedback (
    correlation_id TEXT PRIMARY KEY,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (now())
);

CREATE INDEX IF NOT EXISTS idx_chat_feedback_created_at ON chat_feedback(created_at DESC);
