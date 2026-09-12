-- V2 REACT CONTRACT + THREAD EVIDENCE LEDGER
--
-- Ananth, 2026-09-12: "lets get the contract for react structured.. this will
-- be immensely useful telemetry as we start to store it. over time this is
-- gold.. we also need it to store the facts/things it found useful vs not
-- useful (so that we dont have to send it again)".
--
-- TWO TABLES, TWO LIFETIMES, and conflating them is why neither exists today:
--
--   react_v2_rounds    one row per ROUND. Append-only history: what react
--                      claimed at that moment. Never updated -- a claim that
--                      gets corrected later is a SECOND row, because the
--                      disagreement between them is the telemetry.
--
--   thread_evidence    one row per (thread, source). Current state: is this
--                      source useful to this thread or not. Updated in place,
--                      because "we already decided this document is useless"
--                      is a fact about now, not a history.
--
-- The second is what stops us re-sending. A fact costs ~100 chars to carry
-- across a turn; the passage it came from costs ~9,000. Measured: one preload
-- is 141,074 characters and the answer it supports is three facts.

CREATE TABLE IF NOT EXISTS react_v2_rounds (
    correlation_id      TEXT        NOT NULL,
    round_index         INTEGER     NOT NULL,
    thread_id           TEXT,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    contract_version    INTEGER     NOT NULL,
    -- WHICH SHAPE ARRIVED. The live prompt still asks for v1's shape, so a
    -- column that assumed v2 would make the migration invisible. v2 | v1 |
    -- mixed | none -- and "none" is a real answer, not a null.
    shape_seen          TEXT        NOT NULL DEFAULT 'none',
    thought             TEXT,
    roles_assumed       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    facts               JSONB       NOT NULL DEFAULT '[]'::jsonb,
    not_useful          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    gaps                JSONB       NOT NULL DEFAULT '[]'::jsonb,
    running_answer      TEXT,
    answer_chars        INTEGER,
    tool_request        TEXT,
    tool_reason         TEXT,
    -- NULLABLE ON PURPOSE: react not saying is different from react saying
    -- false, and a NOT NULL DEFAULT false would erase that distinction on
    -- every row where the model simply did not answer.
    is_complete         BOOLEAN,
    complete_why        TEXT,
    next_round_worth_it BOOLEAN,
    next_round_why      TEXT,
    -- What could not be read out of the response. A turn that returned an
    -- unparseable shape is exactly the turn worth finding later.
    problems            JSONB       NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (correlation_id, round_index)
);

CREATE INDEX IF NOT EXISTS idx_react_v2_rounds_thread ON react_v2_rounds (thread_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_react_v2_rounds_shape  ON react_v2_rounds (shape_seen, ts DESC);
-- The question this table exists to answer: how often did react say another
-- round was worth it, and was it right?
CREATE INDEX IF NOT EXISTS idx_react_v2_rounds_worth  ON react_v2_rounds (next_round_worth_it)
    WHERE next_round_worth_it IS NOT NULL;

COMMENT ON TABLE react_v2_rounds IS
  'Append-only: one row per react round, v2 contract. Claims at a moment; the '
  'value is the disagreement between them over time (is_complete vs gaps still '
  'open, next_round_worth_it vs what the next round closed).';


CREATE TABLE IF NOT EXISTS thread_evidence (
    thread_id       TEXT        NOT NULL,
    -- document + page, normalised by the writer. Page NULL means the verdict
    -- is about the whole document.
    document_name   TEXT        NOT NULL,
    page_number     INTEGER,
    -- useful | not_useful. No third value: "we have not looked" is the ABSENCE
    -- of a row, and giving it an enum value would make could-not-check
    -- indistinguishable from checked-false -- the collapse this whole module
    -- exists to remove.
    verdict         TEXT        NOT NULL CHECK (verdict IN ('useful', 'not_useful')),
    -- The FACT itself when useful: this is what gets re-sent instead of the
    -- passage, and it is the entire point of the table.
    fact            TEXT,
    why             TEXT,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_seen      INTEGER     NOT NULL DEFAULT 1,
    -- Which turn last wrote this, so a verdict can be traced to the round that
    -- made it rather than floating free.
    correlation_id  TEXT,
    evidence_id     BIGSERIAL   PRIMARY KEY
);

-- Identity is (thread, document, page) with page NULL meaning "the whole
-- document". Postgres will not take an expression in a PRIMARY KEY, so the
-- real key is this unique index and ON CONFLICT must name the SAME
-- expression. Without COALESCE, two rows with NULL page would both insert --
-- NULL is not equal to NULL -- and the ledger would slowly fill with
-- duplicate verdicts about one document, which is exactly the state it exists
-- to prevent.
CREATE UNIQUE INDEX IF NOT EXISTS uq_thread_evidence_source
    ON thread_evidence (thread_id, document_name, COALESCE(page_number, -1));

CREATE INDEX IF NOT EXISTS idx_thread_evidence_lookup
    ON thread_evidence (thread_id, verdict);

COMMENT ON TABLE thread_evidence IS
  'Current state per (thread, source): useful (with the fact) or not_useful. '
  'Read before a turn so known-useless sources are not re-retrieved and known '
  'facts are re-sent as text instead of passages. Updated in place — the '
  'history of how a verdict changed lives in react_v2_rounds.';
