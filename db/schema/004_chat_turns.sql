-- Chat turns: one row per request for history and left panel (recent, most helpful).
-- Same DB as chat_feedback (CHAT_RAG_DATABASE_URL). "Most helpful" = join with chat_feedback where rating = 'up'.

CREATE TABLE IF NOT EXISTS chat_turns (
    correlation_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    thinking_log JSONB,
    final_message TEXT,
    sources JSONB,
    duration_ms INT,
    model_used TEXT,
    llm_provider TEXT,
    session_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (now())
);

CREATE INDEX IF NOT EXISTS idx_chat_turns_created_at ON chat_turns(created_at DESC);
