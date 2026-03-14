-- Improvement 5: result cache layer — stores last tool payload per (thread_id, tool_hint).
-- Enables follow-up messages like "filter those results by Florida" to access the prior tool output.
CREATE TABLE IF NOT EXISTS chat_tool_results (
    thread_id   TEXT        NOT NULL,
    turn_id     TEXT        NOT NULL,
    tool_hint   TEXT        NOT NULL,
    payload     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (thread_id, tool_hint)
);

CREATE INDEX IF NOT EXISTS idx_tool_results_thread
    ON chat_tool_results(thread_id, created_at DESC);
