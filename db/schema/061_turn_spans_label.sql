-- Migration 061: span label + node-key binding for turn_spans.
--
-- `module` is now a NODE KEY from the chat schema (the 36 keys in
-- scripts/platform/chat_node_content.py), not an ad-hoc name. Ad-hoc names
-- gave two independent decompositions of one system — 36 schema nodes and N
-- arbitrary spans — with neither able to check the other. Binding them makes
-- the two reciprocally falsifiable:
--
--   a span whose module is not a node  -> the schema is incomplete
--   a live node that never produces a span -> dead code, or mis-modelled
--
-- `label` carries spans finer than any node (one tool call inside react_loop,
-- one write inside state_load). Those inherit the parent's node key and keep
-- their local name here, so the mapping is node -> span TREE rather than
-- node -> span, and every span still resolves to a node.

ALTER TABLE turn_spans ADD COLUMN IF NOT EXISTS label TEXT;

-- Node-level rollup is the query the schema page will run to put real p50/p95
-- and real counts next to each node's findings and readiness rating.
CREATE INDEX IF NOT EXISTS turn_spans_node_idx ON turn_spans (module, created_at DESC);
