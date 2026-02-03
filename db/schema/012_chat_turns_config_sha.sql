-- Add config_sha to chat_turns so each turn is tied to the prompts+LLM config version used for that run (auditability).

ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS config_sha TEXT;
