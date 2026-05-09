-- migrations/035_user_tool_subscriptions.sql
-- Per-user tool policy persistence (2026-05-09).
--
-- Stores each user's opt-in/opt-out decision for individual chat tools.
-- The pipeline resolves ctx.allowed_tools by combining:
--
--   1. Mode default (task mode → [], all other modes → all tools)
--   2. User subscriptions (rows here override the mode default per tool)
--   3. Per-request policy (ChatRequest.tool_policy, a future field)
--
-- A user who has never touched settings has NO rows here — that means
-- "use mode defaults", so the full tool list is available. Only
-- explicit overrides are stored (space-efficient, no bootstrap needed).
--
-- Design choices:
--   * ``user_id`` matches auth.user_id from mobius-user JWT (TEXT, not
--     UUID — consistent with how we store it in chat_turns.user_id).
--   * ``tool_name`` is the canonical registry key (snake_case, same string
--     the planner emits). Includes router-owned tools (search_corpus,
--     healthcare_npi_lookup, search_uploaded_document, refuse) which
--     aren't in the SkillSpec registry but ARE blockable via policy.
--   * ``enabled`` boolean: True = user wants this tool on, False = off.
--     Defaults to True so an explicit "off" is intentional and visible
--     in the audit trail.
--   * ``updated_at`` for UI "last changed" display and future sync
--     conflict resolution.
--
-- Not FK-linked to users table — mobius-user is in a separate DB.
-- Orphan rows (user deleted) are benign; a monthly cleanup job can
-- prune them once user lifecycle is formalized.

CREATE TABLE IF NOT EXISTS user_tool_subscriptions (
    user_id     TEXT         NOT NULL,
    tool_name   TEXT         NOT NULL,
    enabled     BOOLEAN      NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    PRIMARY KEY (user_id, tool_name)
);

-- Index for the common read path: load all tool prefs for one user.
CREATE INDEX IF NOT EXISTS idx_uts_user_id
    ON user_tool_subscriptions (user_id);

-- Index for analytics: which tools are most commonly disabled?
CREATE INDEX IF NOT EXISTS idx_uts_tool_disabled
    ON user_tool_subscriptions (tool_name)
    WHERE enabled = FALSE;

COMMENT ON TABLE user_tool_subscriptions IS
    'Per-user opt-in/opt-out for individual chat tools. '
    'Absence of a row means "use mode default" (not "enabled"). '
    'Only explicit overrides are stored.';

COMMENT ON COLUMN user_tool_subscriptions.user_id IS
    'Matches auth.user_id from mobius-user JWT. TEXT (not UUID) for consistency with chat_turns.user_id.';
COMMENT ON COLUMN user_tool_subscriptions.tool_name IS
    'Canonical tool name: the snake_case key the planner emits. '
    'Includes registry skills and router-owned tools (search_corpus, etc.).';
COMMENT ON COLUMN user_tool_subscriptions.enabled IS
    'True = user wants this tool available. False = user disabled it. '
    'NULL is not allowed — absence of a row means "use default", not NULL.';
