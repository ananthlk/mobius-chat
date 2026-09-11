-- Migration 066: exact per-tokenizer token counts on prompt_blocks.
--
-- Governor seat's estimate() correction (2026-09-10, session thread on
-- docs/governor-prompt-budget.md): chars//4 is a heuristic whose error is
-- content-and-tokenizer-dependent (measured both directions on real content
-- this session — 3% over on Gemini for prose-heavy react.critical_rules,
-- 5% under on Gemini for manifest-shaped text) rather than a fixed
-- correction factor. Blocks are immutable once versioned (053's append-only
-- design), so an exact count is compute-once-at-publish and strictly better
-- than any heuristic, in either direction, forever after.
--
-- token_counts is a MAP, not a single integer: a composition can be served
-- by whichever model the bandit routes to (Vertex/Anthropic/Groq all live
-- candidates per model_registry's roster), and a block's real token count
-- differs by tokenizer. Governor's ruling: budget using MAX across the
-- stored tokenizers for the candidate pool, not the count for whichever
-- model the bandit eventually picks -- the bound must hold before the
-- choice is made, and an under-count fails open (permits a prompt that
-- doesn't fit) while an over-count only fails closed (conservative) --
-- given today's pattern of fail-open defects, conservative wins ties.
--
-- Keys are tokenizer identifiers, e.g. {"gemini": 3093, "anthropic": 3210}.
-- Missing key = no stored count for that tokenizer; callers fall back to
-- chars//4 and must record that the number came from a fallback, not a
-- stored exact count (provenance travels with the estimate, not just the
-- estimate itself).

ALTER TABLE prompt_blocks
    ADD COLUMN IF NOT EXISTS token_counts JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN prompt_blocks.token_counts IS
    'Exact per-tokenizer token count for this block''s template_body, '
    'computed once at publish time (blocks are immutable per-version). '
    'Map of tokenizer_id -> int, e.g. {"gemini": 3093}. Absent key means '
    'no stored count for that tokenizer -- caller must fall back to a '
    'heuristic AND record that fallback happened.';
