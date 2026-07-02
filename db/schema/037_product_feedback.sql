-- 037 — product feedback skill (open + satisfaction-survey instruments).
--
-- Complements the turn-scoped thumbs (chat_feedback, migration 003). This is
-- open product feedback + CSAT/CES/NPS surveys, captured by the product_feedback
-- skill. Persistence is chat-side (app/storage/product_feedback.py); the
-- standalone mobius-feedback service only classifies.
--
-- Three altitudes of feedback now coexist:
--   turn         → chat_feedback (thumbs, exists)
--   thread       → product_feedback kind='survey' survey_type='csat'
--   relationship → product_feedback kind='survey' survey_type='nps'
--   any          → product_feedback kind='open'  (categorical + free text)
--
-- Safe to run repeatedly (IF NOT EXISTS throughout). No dependency on the code
-- deploy: the storage layer degrades to a log line in dev if this hasn't run.

-- ── items ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS product_feedback (
    feedback_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger             TEXT NOT NULL DEFAULT 'on_demand',   -- inline | periodic | on_demand
    status              TEXT NOT NULL DEFAULT 'captured',    -- draft|captured|triaged|routed|closed

    -- instrument
    kind                TEXT NOT NULL DEFAULT 'open',        -- open | survey
    survey_type         TEXT,                                -- csat | ces | nps
    score               NUMERIC,                             -- null for open
    score_scale         TEXT,                                -- nps_0_10 | csat_1_5 | ces_1_5
    parent_feedback_id  UUID REFERENCES product_feedback(feedback_id),

    -- context / identity
    thread_id           UUID,
    correlation_id      TEXT,
    user_id             TEXT,
    org_slug            TEXT,

    -- classification (open instrument; also the survey follow-up)
    category            TEXT,
    sentiment           TEXT DEFAULT 'neutral',
    severity            TEXT DEFAULT 'low',
    summary             TEXT,
    verbatim            TEXT,
    tidied              TEXT,
    area_tags           JSONB DEFAULT '[]',

    -- routing
    routed_to           TEXT,
    linked_task_id      TEXT,

    -- provenance
    config_sha          TEXT,
    source_context_hash TEXT,
    usage               JSONB DEFAULT '{}',
    extra               JSONB DEFAULT '{}',

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- a survey row must carry a score+type; an open row must carry a category
    CONSTRAINT product_feedback_kind_shape CHECK (
        (kind = 'survey' AND score IS NOT NULL AND survey_type IS NOT NULL)
        OR (kind = 'open' AND category IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS product_feedback_user_idx
    ON product_feedback(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS product_feedback_category_idx
    ON product_feedback(category, created_at DESC);
CREATE INDEX IF NOT EXISTS product_feedback_status_idx
    ON product_feedback(status) WHERE status <> 'closed';
CREATE INDEX IF NOT EXISTS product_feedback_survey_idx
    ON product_feedback(survey_type, created_at DESC) WHERE kind = 'survey';

-- ── per-user cadence state (one row per user, upsert) ──────────────────────
CREATE TABLE IF NOT EXISTS feedback_prompt_state (
    user_id               TEXT PRIMARY KEY,
    threads_since_prompt  INT NOT NULL DEFAULT 0,
    turns_since_prompt    INT NOT NULL DEFAULT 0,
    last_prompted_at      TIMESTAMPTZ,
    last_captured_at      TIMESTAMPTZ,
    last_csat_at          TIMESTAMPTZ,
    last_nps_at           TIMESTAMPTZ,
    snooze_until          TIMESTAMPTZ,
    opted_out             BOOLEAN NOT NULL DEFAULT false,
    prompt_count          INT NOT NULL DEFAULT 0,
    capture_count         INT NOT NULL DEFAULT 0,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── append-only funnel log ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS feedback_prompt_events (
    event_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT,
    thread_id   UUID,
    trigger     TEXT NOT NULL,               -- inline | periodic | on_demand
    kind        TEXT,                        -- open | csat | nps
    action      TEXT NOT NULL,               -- shown|opened|scored|submitted|dismissed|snoozed|opted_out
    category    TEXT,
    score       NUMERIC,
    feedback_id UUID REFERENCES product_feedback(feedback_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS feedback_prompt_events_funnel_idx
    ON feedback_prompt_events(trigger, action, created_at DESC);

-- ── health view (open funnel + satisfaction) ───────────────────────────────
CREATE OR REPLACE VIEW feedback_health AS
SELECT
    date_trunc('day', created_at)                                    AS day,
    kind,
    survey_type,
    category,
    count(*)                                                        AS n,
    avg(score) FILTER (WHERE kind = 'survey')                       AS avg_score,
    -- NPS: %promoters (9-10) − %detractors (0-6), over nps rows that day
    (100.0 * count(*) FILTER (WHERE survey_type = 'nps' AND score >= 9)
        / NULLIF(count(*) FILTER (WHERE survey_type = 'nps'), 0))
      - (100.0 * count(*) FILTER (WHERE survey_type = 'nps' AND score <= 6)
        / NULLIF(count(*) FILTER (WHERE survey_type = 'nps'), 0))   AS nps,
    -- CSAT: %top-2-box (4-5 on 1-5)
    100.0 * count(*) FILTER (WHERE survey_type = 'csat' AND score >= 4)
        / NULLIF(count(*) FILTER (WHERE survey_type = 'csat'), 0)   AS csat_pct
FROM product_feedback
GROUP BY 1, 2, 3, 4;
