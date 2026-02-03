-- Link request-level chat_turns to thread. Run after 008_chat_threads.

ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS thread_id UUID REFERENCES chat_threads(thread_id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_chat_turns_thread_id ON chat_turns(thread_id) WHERE thread_id IS NOT NULL;
