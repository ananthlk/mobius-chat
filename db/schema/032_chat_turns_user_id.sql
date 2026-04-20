-- Phase 2d completion (2026-04-19): add user_id column to chat_turns.
--
-- Phase 1h shipped CHAT_AUTH_MODE + require_user dependency.
-- Phase 2d (commit c3f7327) applied require_user to 10 write endpoints
-- but named the returned user_id `_user_id` and discarded it. This
-- migration + the code change in commits 2613e14..b9106b1 complete
-- the loop by stamping the authenticated user_id onto each chat_turns
-- row for audit attribution.
--
-- Safe to run at any time. Nullable column, no default — existing
-- rows stay NULL; new rows get the user_id when auth is enabled,
-- NULL otherwise (dev mode, CHAT_AUTH_MODE=off).
--
-- Graceful fallback: the Python code in app/storage/turns.py and
-- app/persistence/postgres.py catches the "column does not exist"
-- error and retries the INSERT without user_id. That means this
-- migration can be applied independently of the code deploy — chat
-- keeps working before, during, and after.

ALTER TABLE chat_turns ADD COLUMN IF NOT EXISTS user_id TEXT;

-- Index for per-user audit queries. Nullable so existing NULL rows
-- don't block the index; partial would be slightly smaller but
-- partial indexes miss the "where user_id IS NULL" queries the
-- audit dashboard might want.
CREATE INDEX IF NOT EXISTS idx_chat_turns_user_id ON chat_turns(user_id);
