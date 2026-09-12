-- 070_turn_compliance.sql — did react follow the instruction, or ignore it?
--
-- Ananth, 2026-09-12: "what we need is a set of telemetry on what instructions
-- the llm follows vs what it ignores over time."
--
-- ONE ROW PER (turn, statement, round). That grain is what makes "over time"
-- answerable: a follow rate per statement id, trended, is the signal that says
-- which wording to rewrite and which to delete.
--
-- COMPLIANCE IS OBSERVED FROM THE TRACE, NEVER ASKED OF THE MODEL. A
-- "did you follow it?" field would be the most flattering number in the
-- product and the least true — this system has already shipped a model
-- asserting "no sources found" beside a confident answer.
--
-- FOUR VERDICTS, NOT THREE. `unobservable` is a first-class answer and is
-- EXCLUDED from any rate: "we could not check" and "it ignored us" are
-- opposite facts — the first says fix the telemetry, the second says fix the
-- statement — and collapsing them would quietly condemn instructions that were
-- obeyed. `declined` exists because the governor's dissent is answerable
-- either way, and counting a legitimate refusal as disobedience would
-- contradict the design (Ananth: "not to do so unilaterally").

CREATE TABLE IF NOT EXISTS turn_compliance (
    correlation_id  TEXT        NOT NULL,
    round_index     INT         NOT NULL,
    -- 'statement' = a governor instruction; 'ack' = react's own claim about
    -- what it was doing, checked against what the round did.
    kind            TEXT        NOT NULL CHECK (kind IN ('statement','ack')),
    -- statement id (CTL-1, FRM-2 ...) or ack key (parts, working_gap, dissent)
    item            TEXT        NOT NULL,
    verdict         TEXT        NOT NULL
                    CHECK (verdict IN ('followed','ignored','unobservable','declined')),
    -- What react CLAIMED and what the trace SHOWS, kept apart on purpose: the
    -- DISAGREEMENT is the signal, and a single merged field cannot express it.
    claimed         TEXT,
    observed        TEXT,
    -- In words, so a reader can argue with the verdict instead of trusting it.
    basis           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (correlation_id, round_index, kind, item)
);

CREATE INDEX IF NOT EXISTS turn_compliance_item_idx
    ON turn_compliance (item, verdict, created_at DESC);
