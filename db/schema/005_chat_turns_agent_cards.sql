-- Agent outputs and I/O cards per turn (plan snapshot, blueprint, agent cards).
-- Same DB as chat_turns (CHAT_RAG_DATABASE_URL). Run after 004_chat_turns.

ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS plan_snapshot JSONB;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS blueprint_snapshot JSONB;
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS agent_cards JSONB;
