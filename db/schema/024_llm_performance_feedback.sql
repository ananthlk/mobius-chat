-- Model-efficiency / router feedback (separate from answer-quality thumbs).
-- Run on same DB as chat_feedback (CHAT_RAG_DATABASE_URL).

CREATE TABLE IF NOT EXISTS llm_performance_feedback (
    correlation_id TEXT NOT NULL PRIMARY KEY,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (now())
);

CREATE INDEX IF NOT EXISTS idx_llm_performance_feedback_created ON llm_performance_feedback(created_at);
