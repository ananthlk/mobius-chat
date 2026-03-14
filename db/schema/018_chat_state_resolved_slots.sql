-- Improvement 3: persist resolved slot values so planner never loses jurisdiction after slot fill.
ALTER TABLE chat_state ADD COLUMN IF NOT EXISTS resolved_slots JSONB NOT NULL DEFAULT '{}';
