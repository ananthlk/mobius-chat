-- Two-tier rolling thread summary, canonical per-thread (one row).
--
-- Background: Phase 13.7 stored ONE summary field per *turn* on
-- chat_turns.context_summary, and the integrator was prompted to emit a
-- ≤60-char sidebar label. That conflated two jobs (short sidebar label
-- vs rich rolling context) into one field, and the sidebar header still
-- rendered chat_threads.title (frozen to the first question), so the
-- rolling value never appeared to update.
--
-- This migration splits the two tiers onto chat_threads so there is
-- exactly ONE authoritative summary per thread, updated in place each
-- turn (see storage/threads.upsert_thread_summary):
--   * summary_short — rolling sidebar label (the subject as it stands now)
--   * summary_long  — rolling rich context fed back to the integrator
--                     (payer/jurisdiction, codes, URLs, form names,
--                      answered-vs-still-wanted)
--
-- chat_turns.context_summary is intentionally LEFT IN PLACE: it remains a
-- per-turn history snapshot for telemetry/eval. The sidebar + integrator
-- read the canonical per-thread columns added here, falling back to the
-- per-turn value for legacy threads.
--
-- Run after 035_user_tool_subscriptions.sql.

ALTER TABLE chat_threads ADD COLUMN IF NOT EXISTS summary_short TEXT;
ALTER TABLE chat_threads ADD COLUMN IF NOT EXISTS summary_long  TEXT;
