-- Short-term state per thread (payer, domain, open_slots, etc.). Run after 008_chat_threads.

CREATE TABLE IF NOT EXISTS chat_state (
    thread_id UUID PRIMARY KEY REFERENCES chat_threads(thread_id) ON DELETE CASCADE,
    state_json JSONB NOT NULL DEFAULT '{}',
    state_version INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT (now())
);

CREATE INDEX IF NOT EXISTS idx_chat_state_updated_at ON chat_state(updated_at DESC);
