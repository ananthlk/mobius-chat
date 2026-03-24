-- Technical user feedback on post-run adjudicator / QA scorecard (separate from answer-quality chat_feedback).
-- Same DB as chat_feedback (CHAT_RAG_DATABASE_URL).

CREATE TABLE IF NOT EXISTS adjudication_feedback (
    correlation_id TEXT NOT NULL PRIMARY KEY,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (now())
);

CREATE INDEX IF NOT EXISTS idx_adjudication_feedback_created ON adjudication_feedback(created_at);
