-- Persist credentialing co-pilot runs so POST /validate (API) sees runs created by the chat worker.
CREATE TABLE IF NOT EXISTS credentialing_runs (
    run_id TEXT PRIMARY KEY,
    body JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_credentialing_runs_updated ON credentialing_runs (updated_at DESC);
