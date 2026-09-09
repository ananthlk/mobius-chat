-- Migration 060: per-module turn spans (P2b latency telemetry).
--
-- Ananth's ask: catch bad writes, bad DB calls and loops; don't drift.
-- One row per span per turn; the counts a span accumulated live in JSONB on
-- that row rather than a child table, because they are always read with
-- their span and never independently.
--
-- WHY counts is an ARRAY OF OBJECTS and not a {target: n} map:
-- a bare integer cannot separate "40 writes to chat_state" (a loop) from
-- "40 writes across 40 tables" (a busy turn), and the loop is the target.
-- Each element is {kind, target, n, ms} so n=40/ms=80 (forty 2ms writes)
-- stays distinguishable from n=1/ms=80 (one slow call).
--
-- wall_ms vs llm_ms: llm_ms is measured IN-PROCESS inside the span, not
-- joined from llm_calls. As of 2026-09-09, 790 of 1,979 llm_calls rows in
-- 24h had a NULL correlation_id (phi_classify, rag_fact_check, parser and
-- integrator at 0%), so that join returns a partial set with no signal that
-- it is partial. self_ms = wall_ms - llm_ms is the code-latency number and
-- stays correct whether or not that table is ever repaired.

CREATE TABLE IF NOT EXISTS turn_spans (
    correlation_id   TEXT        NOT NULL,
    span_id          TEXT        NOT NULL,
    parent_span_id   TEXT,
    module           TEXT        NOT NULL,
    depth            INT         NOT NULL DEFAULT 0,
    wall_ms          NUMERIC(12,2) NOT NULL DEFAULT 0,
    llm_ms           NUMERIC(12,2) NOT NULL DEFAULT 0,
    counts           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- Stamp per {provider, model}, not per provider: within Vertex, flash
    -- (~4.7s) and Pro (~20s) are 4x apart, so the model is the controlling
    -- dimension for any run-to-run comparison.
    model_mix        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- Records whether react_loop's evidence-threshold early exit fired
    -- (_FAST_MODE_MIN_CHUNKS/_CHARS). It changes ROUND COUNT and is
    -- triggered by corpus content, so it moves between runs with no config
    -- difference to point at. Unrecorded, a run where 8 of 22 questions took
    -- the early exit is silently incomparable to one where 3 did.
    rich_evidence    BOOLEAN,
    chat_mode        TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (correlation_id, span_id)
);

CREATE INDEX IF NOT EXISTS turn_spans_cid_idx     ON turn_spans (correlation_id);
CREATE INDEX IF NOT EXISTS turn_spans_created_idx ON turn_spans (created_at DESC);
CREATE INDEX IF NOT EXISTS turn_spans_module_idx  ON turn_spans (module, created_at DESC);
