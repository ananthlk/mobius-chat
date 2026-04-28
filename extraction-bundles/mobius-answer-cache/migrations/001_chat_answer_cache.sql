-- mobius-answer-cache · Phase 1 schema
-- ===================================================================
--
-- The cache table that pgvector backs. Run against either a sibling
-- mobius_cache database or alongside mobius_rag's tables (see open
-- question §6.1 in docs/SPEC.md).
--
-- Required extensions: pgvector (CREATE EXTENSION IF NOT EXISTS vector).
--
-- Embedding dimension MUST match what the embedding service emits.
-- Today's value (1536) is for gemini-embedding-001 which is what
-- chat + rag both use. If the embedding model changes, do NOT
-- ALTER COLUMN — make a new column and run a re-embed migration.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chat_answer_cache (
  id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  correlation_id  UUID         UNIQUE NOT NULL,                -- chat turn cid; idempotency key
  thread_id       UUID         NULL,
  question        TEXT         NOT NULL,
  question_norm   TEXT         NOT NULL,                       -- normalized form for FTS / dedupe
  embedding       vector(1536) NOT NULL,
  answer          TEXT         NOT NULL,
  skill_envelope  JSONB        NOT NULL,                       -- full SkillEnvelope to replay later
  config_sha      TEXT         NULL,
  payer           TEXT         NULL,
  state           TEXT         NULL,                           -- canonical 2-letter (FL / CA / …)
  program         TEXT         NULL,
  authority_level TEXT         NULL,
  domain_tags     TEXT[]       NOT NULL DEFAULT '{}',
  qc_passed       BOOLEAN      NOT NULL,
  thumbs_down     BOOLEAN      NOT NULL DEFAULT false,
  thumbs_down_reason TEXT      NULL,
  caller          TEXT         NULL,                           -- "mobius_chat" / "mobius_chat_bench" / extension callers
  caller_id       TEXT         NULL,                           -- request-id passed via X-Caller-Id header
  answered_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  invalidated_at  TIMESTAMPTZ  NULL,                           -- soft-delete via bulk_invalidate
  invalidated_reason TEXT      NULL
);

-- ANN index for similarity lookups. HNSW is right for this workload
-- (read-heavy with frequent inserts; IVF would need re-training).
CREATE INDEX IF NOT EXISTS cac_embedding_hnsw_idx
  ON chat_answer_cache
  USING hnsw (embedding vector_cosine_ops);

-- History query indexes
CREATE INDEX IF NOT EXISTS cac_thread_idx
  ON chat_answer_cache (thread_id, answered_at DESC)
  WHERE thread_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS cac_caller_idx
  ON chat_answer_cache (caller, answered_at DESC)
  WHERE caller IS NOT NULL;

-- Filter pushdown (lookup filters by jurisdiction)
CREATE INDEX IF NOT EXISTS cac_filter_idx
  ON chat_answer_cache (payer, state, program, answered_at DESC)
  WHERE invalidated_at IS NULL;

-- For invalidation queries (find rows by config_sha, state, etc.)
CREATE INDEX IF NOT EXISTS cac_config_sha_idx
  ON chat_answer_cache (config_sha)
  WHERE config_sha IS NOT NULL AND invalidated_at IS NULL;

-- For "is this row eligible for lookup" common-case filter
CREATE INDEX IF NOT EXISTS cac_eligible_idx
  ON chat_answer_cache (answered_at DESC)
  WHERE thumbs_down = false AND invalidated_at IS NULL;

-- Optional: GIN on domain_tags for tag-based filtering
CREATE INDEX IF NOT EXISTS cac_domain_tags_gin
  ON chat_answer_cache
  USING gin (domain_tags);


-- ── Notes for the agent ──────────────────────────────────────────────
--
-- 1. ``correlation_id`` UNIQUE constraint enforces idempotency.
--    On replay, write_handler() should:
--      INSERT … ON CONFLICT (correlation_id) DO UPDATE
--        SET answer = EXCLUDED.answer,
--            skill_envelope = EXCLUDED.skill_envelope,
--            answered_at = EXCLUDED.answered_at
--        RETURNING id;
--    Returning EXCLUDED.id keeps the same candidate_id across replays.
--
-- 2. ``invalidated_at`` is soft-delete. ``bulk_invalidate()`` should
--    UPDATE ... SET invalidated_at = now() rather than DELETE so
--    history queries can still see superseded answers if needed.
--    The eligible-for-lookup partial indexes already filter these out.
--
-- 3. ``question_norm`` is for future FTS / dedupe work (the rag
--    agent's bm25_normalized_query pattern). Phase 1 can write a
--    pass-through (lower(question)); Phase 2 can wire a real
--    normalizer.
