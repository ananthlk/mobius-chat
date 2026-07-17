-- 043 — training-mode telemetry.
--
-- Append-only log of training-mode outcomes and graduation actions.
-- Four event types:
--   training_completed         — user finished all 5 steps → graduation
--   training_skipped           — user clicked "skip" on the consent screen (step 0, temp dismiss)
--   training_dismissed         — user clicked × before reaching graduation (permanent dismiss via _finishOnboarding)
--   graduation_question_fired  — user submitted a question from the graduation screen
--
-- Supports two PA metrics:
--   D1/D7 return rate (trained vs skipped) — join user_id against UM's login log
--   First-question fire rate                — graduation_question_fired vs training_completed

CREATE TABLE IF NOT EXISTS training_events (
    event_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL,
    event_type  TEXT NOT NULL,   -- training_completed|training_skipped|training_dismissed|graduation_question_fired
    source      TEXT,            -- chip|typed  (graduation_question_fired only)
    text        TEXT,            -- question text (graduation_question_fired only)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS training_events_user_idx
    ON training_events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS training_events_type_idx
    ON training_events(event_type, created_at DESC);

-- ── analytics views ─────────────────────────────────────────────────────────
-- Daily outcome counts (feed into D1/D7 return-rate joins against UM login log).
CREATE OR REPLACE VIEW training_outcome_summary AS
SELECT
    event_type,
    date_trunc('day', created_at) AS day,
    count(DISTINCT user_id)        AS users,
    count(*)                       AS events
FROM training_events
WHERE event_type IN ('training_completed', 'training_skipped', 'training_dismissed')
GROUP BY 1, 2
ORDER BY 2 DESC, 1;

-- Graduation funnel: how many completers fired the first question, typed vs chip.
CREATE OR REPLACE VIEW training_graduation_funnel AS
SELECT
    c.day,
    c.users                                                             AS graduated,
    coalesce(f.total_fires, 0)                                          AS questions_fired,
    coalesce(f.typed_fires, 0)                                          AS typed_fires,
    coalesce(f.chip_fires, 0)                                           AS chip_fires,
    round(100.0 * coalesce(f.total_fires, 0) / nullif(c.users, 0), 1)  AS fire_pct
FROM (
    SELECT date_trunc('day', created_at) AS day, count(DISTINCT user_id) AS users
    FROM   training_events
    WHERE  event_type = 'training_completed'
    GROUP  BY 1
) c
LEFT JOIN (
    SELECT
        date_trunc('day', created_at)                    AS day,
        count(*)                                         AS total_fires,
        count(*) FILTER (WHERE source = 'typed')         AS typed_fires,
        count(*) FILTER (WHERE source = 'chip')          AS chip_fires
    FROM   training_events
    WHERE  event_type = 'graduation_question_fired'
    GROUP  BY 1
) f USING (day)
ORDER BY 1 DESC;
