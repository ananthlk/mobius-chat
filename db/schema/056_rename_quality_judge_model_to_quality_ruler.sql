-- Migration 056: rename quality_judge_model -> quality_ruler (mig 055).
--
-- Naming got crossed in cross-session coordination: I shipped 055 with
-- quality_judge_model after an earlier ambiguous "Database prefers X,
-- confirm if you have a preference" message. Two subsequent, unambiguous
-- messages from Database (via Chat Master) confirmed quality_ruler is the
-- actual name -- Eval and Bandit Agent are both waiting on this exact name
-- before writing byte-for-byte matching code. Renaming now while it's cheap
-- (055 shipped minutes ago, zero real rows ever wrote to the column -- only
-- a synthetic test row, already cleaned up).

ALTER TABLE llm_calls RENAME COLUMN quality_judge_model TO quality_ruler;
