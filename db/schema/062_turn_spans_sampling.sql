-- Migration 062: sampling decision + a recent-traces index.
--
-- `sampled` records WHY a turn has no spans. Without it, "no rows for this
-- cid" means both "not sampled" (expected) and "sampled but recorded
-- nothing" (a bug), which are opposite conclusions from identical evidence.
-- Same absence-with-no-record shape that __none__ and __unfiltered__ exist
-- to prevent, one level up.
--
-- sample_rate is stored per turn so a trace remains interpretable after
-- someone changes the rate: a run at 1.0 and a run at 0.05 are not
-- comparable populations, and the row has to say which it was.
ALTER TABLE turn_spans ADD COLUMN IF NOT EXISTS sampled     BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE turn_spans ADD COLUMN IF NOT EXISTS sample_rate NUMERIC(5,4);

-- Recent-traces list: newest depth-0 spans first.
CREATE INDEX IF NOT EXISTS turn_spans_recent_roots_idx
    ON turn_spans (created_at DESC) WHERE depth = 0;
