-- 044 — add reveal_version to training_events.
--
-- Tracks which A/B arm (A/C/D for now, B when UX ships it) a user was
-- shown during the grand-reveal first-run overlay.  Stored on the
-- training_completed / training_skipped / graduation_question_fired rows
-- so the arm can be sliced into every existing funnel query with a simple
-- WHERE / GROUP BY reveal_version.

ALTER TABLE training_events
    ADD COLUMN IF NOT EXISTS reveal_version CHAR(1);

CREATE INDEX IF NOT EXISTS training_events_reveal_version_idx
    ON training_events (reveal_version, event_type, created_at DESC)
    WHERE reveal_version IS NOT NULL;
