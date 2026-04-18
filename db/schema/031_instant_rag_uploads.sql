-- Phase B.1c — Instant-RAG upload catalog.
--
-- Background: as of Phase B.1, uploads are stored per-thread in a JSONB
-- blob (chat_state.state_json.active.uploaded_files[]). That works for
-- "current thread" queries but falls over for everything else:
--   - Can't list "all uploads for user X" without scanning every thread.
--   - Can't find expiring docs without scanning every thread.
--   - Can't audit ingest history.
--   - Can't do cross-thread "my uploads" picker (Phase B.1e, future).
--   - If the JSONB trims (MAX_THREAD_UPLOAD_RECORDS cap), old records
--     silently disappear even though their Chroma + PG chunks still exist.
--
-- This migration introduces a durable, queryable, per-upload catalog.
-- Chunks themselves stay in published_rag_metadata (Phase 029 wired the
-- flags); this table tracks the *upload record* — the user-facing
-- "I attached CP.BH.124.pdf yesterday" unit.
--
-- Dual-write model during transition: _handle_instant_rag_upload writes
-- both to this table AND to the JSONB blob. ReAct fast-path read
-- (_resolve_upload_document_id) keeps reading JSONB for now — no
-- extra PG round-trip per tool call. Catalog becomes source of truth
-- for cross-thread queries; JSONB is effectively a per-thread cache.
--
-- Columns
-- -------
-- document_id    : primary key; matches published_rag_metadata.document_id
--                  for this upload's chunks. The foreign link.
-- envelope_id    : instant-rag skill's envelope_id. Kept because the
--                  /envelope/{id}/promote endpoint (Phase B.7) uses it.
-- upload_id      : user-facing opaque id surfaced in the UI.
-- thread_id      : which thread the upload originated on. Note plural
--                  threads can later "reference" the same upload via
--                  Phase B.1e (not modeled here yet).
-- user_id        : owner (nullable — Phase 1h auth mode is 'off' in dev,
--                  'required' in prod; only populated when the upload
--                  happens on an authenticated turn).
-- filename       : user-visible name.
-- content_type   : e.g. application/pdf. Useful for future filtering.
-- byte_size      : file size at upload time.
-- chunks_count   : how many chunks landed in published_rag_metadata.
-- status         : 'active' | 'expired' | 'discarded' | 'promoted'.
--                  Transitions:
--                    active → expired   (7-day TTL fires via cleanup cron)
--                    active → discarded (user deletes via UI)
--                    active → promoted  (Phase B.7: batch pipeline runs)
-- suggested_*    : LLM auto-tag output (Phase B.2). Nullable.
-- confirmed_*    : user-reviewed tags (Phase B.3). Nullable.
-- created_at     : when the upload row was inserted.
-- expires_at     : skill-side TTL mirror (INSTANT_RAG_TTL_DAYS).
--                  Cleanup cron reads WHERE expires_at < now() AND status='active'.
-- last_queried_at: optional. Touched when search_uploaded_document hits
--                  this doc. Useful for "most recently used" sorting
--                  in a future picker UI.

CREATE TABLE IF NOT EXISTS instant_rag_uploads (
  document_id         TEXT        PRIMARY KEY,
  envelope_id         TEXT        NOT NULL,
  upload_id           TEXT        NOT NULL,
  thread_id           TEXT        NOT NULL,
  user_id             TEXT,
  filename            TEXT        NOT NULL,
  content_type        TEXT,
  byte_size           BIGINT,
  chunks_count        INTEGER,
  status              TEXT        NOT NULL DEFAULT 'active',

  -- Phase B.2 — LLM-suggested tags (populated at ingest, may be NULL)
  suggested_payer     TEXT,
  suggested_state     TEXT,
  suggested_program   TEXT,
  suggested_authority TEXT,

  -- Phase B.3 — user-reviewed tags
  confirmed_payer     TEXT,
  confirmed_state     TEXT,
  confirmed_program   TEXT,
  confirmed_authority TEXT,

  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at          TIMESTAMPTZ,
  last_queried_at     TIMESTAMPTZ,

  -- Status is a small finite set; enforce it at the DB. Postgres CHECK
  -- constraints are cheap and keep transition bugs from silently
  -- producing rows we can't reason about.
  CONSTRAINT instant_rag_uploads_status_valid
    CHECK (status IN ('active', 'expired', 'discarded', 'promoted')),

  -- Upload_id is user-facing; must be globally unique so the frontend
  -- can refer to it without thread context.
  CONSTRAINT instant_rag_uploads_upload_id_unique UNIQUE (upload_id)
);

-- Per-thread list (the common query from the ReAct loop's state
-- snapshot + the /chat/thread/{id}/uploads endpoint).
CREATE INDEX IF NOT EXISTS idx_instant_rag_uploads_thread
  ON instant_rag_uploads(thread_id)
  WHERE status = 'active';

-- Per-user list (Phase B.1e's "my uploads" picker). Partial index
-- skips rows without a user_id (dev/auth-off uploads) to keep the
-- index tight on the common authed case.
CREATE INDEX IF NOT EXISTS idx_instant_rag_uploads_user
  ON instant_rag_uploads(user_id)
  WHERE status = 'active' AND user_id IS NOT NULL;

-- TTL cleanup cron: "find all docs expiring before now". Partial so
-- expired/discarded/promoted rows don't bloat the scan.
CREATE INDEX IF NOT EXISTS idx_instant_rag_uploads_expires
  ON instant_rag_uploads(expires_at)
  WHERE status = 'active';

-- Auto-update updated_at? Not modeled yet — status transitions should
-- be loud in the app code (mark_status() logs them), and
-- last_queried_at is explicitly touched by the lazy-RAG tool on hits,
-- so a silent updated_at column would be confusing extra machinery.
