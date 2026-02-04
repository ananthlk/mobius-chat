-- Progress events for live stream (thinking, message chunks). Same DB as chat_turns.
-- Worker writes here; API stream polls this table (like RAG chunking_events). No Redis subscribe from API.
-- Run after 012_chat_turns_config_sha.

CREATE TABLE IF NOT EXISTS chat_progress_events (
    id BIGSERIAL PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_progress_events_correlation_created
    ON chat_progress_events(correlation_id, created_at ASC, id ASC);
