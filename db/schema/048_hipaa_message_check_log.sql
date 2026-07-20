-- Migration 048: compliance.hipaa_message_check_log — per-message PHI gate audit trail
--
-- Separate from hipaa_analysis_log (document upload screening). This table
-- records the gate decision for every chat message that triggered a PHI check.
-- Unlike hipaa_analysis_log it is NOT append-only — the action column is set
-- at INSERT time ('blocked'|'overridden'|'passed'), so no UPDATE is needed.
--
-- Write path:
--   app/api/chat.py:post_chat — INSERT once per gate-triggered turn:
--     action='blocked'   when gate blocks and caller has no phi_override flag
--     action='overridden' when gate would block but caller set phi_override=true
--     action='passed'    when gate is clean (logged for audit completeness)
--
-- compliance schema is created by migration 042.

CREATE TABLE IF NOT EXISTS compliance.hipaa_message_check_log (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Turn linkage
    correlation_id      TEXT NOT NULL,
    thread_id           TEXT,
    user_id             TEXT,
    org_slug            TEXT,

    -- Gate decision
    action              TEXT NOT NULL
        CHECK (action IN ('blocked', 'overridden', 'passed')),
    gate                TEXT,
    phi_flag            BOOLEAN,
    identifier_labels   TEXT[] NOT NULL DEFAULT '{}',
    phi_evidence        JSONB,           -- [{category,redacted_span,offset,length}]
    classifier_version  TEXT
);

CREATE INDEX IF NOT EXISTS idx_phi_msg_check_cid
    ON compliance.hipaa_message_check_log (correlation_id);
CREATE INDEX IF NOT EXISTS idx_phi_msg_check_user_ts
    ON compliance.hipaa_message_check_log (user_id, ts DESC)
    WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_phi_msg_check_thread
    ON compliance.hipaa_message_check_log (thread_id)
    WHERE thread_id IS NOT NULL;
