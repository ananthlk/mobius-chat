-- Message-level transcript per thread for context_router (last N turns). Run after 008_chat_threads.

CREATE TABLE IF NOT EXISTS chat_turn_messages (
    turn_id UUID NOT NULL,
    thread_id UUID NOT NULL REFERENCES chat_threads(thread_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT (now()),
    PRIMARY KEY (turn_id, role)
);

CREATE INDEX IF NOT EXISTS idx_chat_turn_messages_thread_created ON chat_turn_messages(thread_id, created_at DESC);
