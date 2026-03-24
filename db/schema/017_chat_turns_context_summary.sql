-- context_summary: short planner-facing summary of the turn (used by insert_turn and thread context).
-- Run after 012_chat_turns_config_sha.

ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS context_summary TEXT;
