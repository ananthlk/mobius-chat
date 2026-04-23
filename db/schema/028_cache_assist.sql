-- migrations/028_cache_assist.sql
-- Cache-assist support (2026-04-23).
--
-- Adds two storage surfaces:
--
--   1. ``chat_turns.cache_mode`` + ``cache_*`` columns so analytics can
--      filter turns by cache activity ("what fraction of turns used
--      cache?"). Five-option enum so we can distinguish:
--        - 'active'   — cache shown to LLM, may have influenced answer
--        - 'shadow'   — cache logged but not shown (A/B bypass bucket)
--        - 'off'      — cache explicitly disabled for this turn
--        - 'none'     — feature off globally or not applicable
--
--   2. ``chat_cache_shadow_log`` — one row per shadow-mode turn,
--      capturing what the cache WOULD have returned versus what the
--      fresh path actually produced. A later job diffs these to
--      compute cache-agreement rates and flag drift.
--
-- Both are additive; existing queries keep working unchanged. No
-- backfill needed — pre-existing rows get NULL/'none' defaults and
-- analytics naturally scope to post-deploy turns.


-- ── Enum for cache_mode ───────────────────────────────────────────
--
-- Using TEXT with a CHECK constraint instead of a Postgres ENUM so
-- we can add new values later without a migration (enum alterations
-- are heavyweight in Postgres).

ALTER TABLE chat_turns
    ADD COLUMN IF NOT EXISTS cache_mode TEXT
        DEFAULT 'none'
        CHECK (cache_mode IN ('active', 'shadow', 'off', 'none'));

ALTER TABLE chat_turns
    ADD COLUMN IF NOT EXISTS cache_candidate_count INTEGER DEFAULT 0;

ALTER TABLE chat_turns
    ADD COLUMN IF NOT EXISTS cache_top_similarity NUMERIC(6, 4);
    -- e.g. 0.9432 ; NULL when no candidates returned

ALTER TABLE chat_turns
    ADD COLUMN IF NOT EXISTS cache_influence TEXT
        DEFAULT 'none'
        CHECK (cache_influence IN ('none', 'partial', 'verbatim', 'rejected', 'unknown'));

CREATE INDEX IF NOT EXISTS idx_chat_turns_cache_mode
    ON chat_turns (cache_mode)
    WHERE cache_mode <> 'none';


-- ── Shadow-log table ──────────────────────────────────────────────
--
-- Populated only for turns with cache_mode='shadow'. Small (few % of
-- traffic) so unpartitioned + single btree index is fine. Retention
-- policy: prune rows > 90 days old via a scheduled job when volume
-- becomes an issue.
--
-- Design choices:
--   * ``correlation_id`` is PRIMARY KEY (not FK — chat_turns may be
--     pruned separately and we want shadow rows to survive for
--     cross-run analysis).
--   * ``cached_candidates`` JSONB carries the full candidate list the
--     skill would have returned, including per-candidate similarity,
--     age, quality flags. Compact enough that 10k rows ~ 10 MB.
--   * ``fresh_final_message`` is whatever the real pipeline produced
--     for comparison.
--   * ``agreement_score`` NULL at write time; a later job can fill
--     it via LLM-as-judge or heuristics without re-running anything.

CREATE TABLE IF NOT EXISTS chat_cache_shadow_log (
    correlation_id      TEXT         PRIMARY KEY,
    ts                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    question            TEXT         NOT NULL,
    config_sha          TEXT,
    cached_candidates   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    fresh_final_message TEXT,
    fresh_sources_count INTEGER,
    fresh_signals       TEXT,
    agreement_score     NUMERIC(5, 4),
    agreement_source    TEXT,   -- 'llm_judge' | 'heuristic' | NULL
    scored_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_chat_cache_shadow_log_ts
    ON chat_cache_shadow_log (ts DESC);

CREATE INDEX IF NOT EXISTS idx_chat_cache_shadow_log_unscored
    ON chat_cache_shadow_log (ts DESC)
    WHERE agreement_score IS NULL;
