-- Source confidence strip: value shown in UI (approved_authoritative, approved_informational, pending, partial_pending, unverified).
-- Run after 005_chat_turns_agent_cards. Same DB as chat_turns.

ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS source_confidence_strip TEXT;
