-- Track which integrator path (S=sequential, P=parallel) handled each turn.
-- Joins to rag_query_decisions via correlation_id for A/B quality comparison.
ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS integrator_mode CHAR(1);
CREATE INDEX IF NOT EXISTS chat_turns_integrator_mode_idx
    ON chat_turns (integrator_mode, created_at DESC)
    WHERE integrator_mode IS NOT NULL;
