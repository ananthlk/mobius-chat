# Task #108 Fix Request — LLM Agent (Backend)

**Filed by:** Chat Master · 2026-08-17  
**Priority:** P1 — "No recent chats yet" shows for all users; history sidebar completely broken

## Bug summary

`GET /chat/history/threads` returns `[]` for every user. The sidebar renders "No recent chats yet" even after completed turns.

## Root cause (identified)

`get_recent_threads()` in `app/storage/threads.py:253-303` runs:

```sql
WITH turn_counts AS (
  SELECT thread_id, COUNT(*) AS n FROM chat_turns
  WHERE thread_id IS NOT NULL AND user_id = :uid GROUP BY thread_id
)
...
WHERE tc.n IS NOT NULL AND tc.n > 0
```

This requires **both**:
1. `chat_turns.thread_id` is non-null at write time
2. A matching `chat_threads` row exists

If either is missing, the JOIN produces all NULLs and the WHERE filters everything out → empty result → "No recent chats yet".

## What to check / fix

In the turn write path (non_patient_rag.py or wherever `chat_turns` rows are inserted):

1. **Is `ensure_thread()` being called before or at turn write?** If not, `chat_threads` row never exists.
2. **Is `thread_id` being set on the `chat_turns` row at write time?** If it's NULL, `turn_counts` CTE returns nothing.
3. **Schema check:** If `chat_threads.title`, `turn_count`, `summary_short`, or `context_summary` (on `chat_turns`) don't exist, `get_recent_threads()` silently returns `[]` (error handler at lines 308-319). Run `\d chat_threads` and `\d chat_turns` to verify schema.

## Acceptance criteria

1. After submitting a query, the chat appears in the sidebar under "Recent"
2. `GET /chat/history/threads?limit=20` returns at least one thread row
3. Verified in production (mobius-chat-ortabkknqa-uc.a.run.app)

## Files

- `app/storage/threads.py:253-303` — query that returns empty
- `app/api/history.py:55-71` — route handler
- Wherever `chat_turns` INSERT happens — this is where thread_id must be set
