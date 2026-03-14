-- Improvement 1: structured turn summaries for planner context.
-- context_summary is a ≤150-token planner-facing summary generated at turn-save time.
-- NULL for turns created before this migration (handled by fallback in context_pack.py).
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS context_summary TEXT;
