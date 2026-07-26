# SPEC — LLMManager (Chat Pipeline Refactor)

**Module owner:** LLM Agent
**Coordinator:** Chat Architecture (worktree `bold-joliot`)
**Sign-off chain:** Chat Architecture → Technical Health (`local_7eb9d965…`) · DB gate → Platform-Architects · Bandit seam → Eval
**Status:** ✅ **BUILD SIGN-OFF GRANTED (Technical Health, 2026-07-25).** All design questions CLOSED; DQ-1 DB gate closed **5/5**. **Build may start.** One condition gates *Migration A landing* (not code): the `module_key` historical backfill — ruled by Eval (§4 / open item below). Migration files (049+) route to Tech Health for spot-check; AC-18's executed A→B gate result routes to Tech Health when it runs.
**Living doc:** decisions recorded here in-place, not in chat.

---

## 0. TL;DR and the one thing reviewers must decide first

LLMManager is **not greenfield.** Chat already has:

- `app/services/llm_manager.py` — a working single-gateway `generate(prompt, stage, …)` that routes models via the Thompson-sampling bandit and logs to `llm_calls`.
- `config/prompts_llm.yaml` + `app/prompts_llm_config.py` — a YAML prompt store (named keys, `str.format()` placeholders, whole-file `config_sha`), **no per-row versioning, no variants, no Jinja2, no EngagementContext tag-matching.**
- `config/model_profiles.yaml` + `app/services/model_registry.py` — per-stage model pins + bandit; **already A/Bs *models*** (`is_ab_call = call_count < 100`, `ab_variant = model_id`).
- `db/schema/020_llm_calls.sql` — analytics table feeding a materialized view (`022_model_performance_view.sql`) and bandit training.

So this refactor is a **consolidation + formalization**, and its single biggest decision is:

> **DQ-1 (blocking): Does `llm_call_log` extend the existing `llm_calls` table (add columns), or is it a new parallel table?**
> A new table orphans `022_model_performance_view` and the bandit's training reads. Recommendation: **extend `llm_calls`** (add `module_key`, `template_id`, `variant_id`, `turn_id`) and expose it under the logical name `llm_call_log`. This needs a Platform-Architects ruling before land (DB gate) and a Model Bandit owner ack (training-read seam).

Everything else in this spec is downstream of that decision.

---

## 1. Contract — the public surface every caller sees

The refactor's real architectural shift: **prompt ownership moves *into* the manager.** Today callers pass an already-rendered `prompt: str`. After the refactor, callers pass a `module_key` + context, and LLMManager fetches the template, renders it, picks the model/config, calls, retries, logs, and returns a typed response.

### 1.1 `LLMResponse` (typed dataclass — the return contract)

```
@dataclass(frozen=True)
class LLMResponse:
    content: str                 # model output text
    model_used: str              # e.g. "gemini-2.5-flash" (resolved, post-fallback)
    template_id: int             # prompt_templates.id actually rendered
    variant_id: str              # prompt variant actually served ("default" when no A/B)
    call_id: str                 # UUID, PK of the llm_call_log row
    latency_ms: int
    tokens_used: int             # = tokens_input + tokens_output (DQ-5 CLOSED — sum exposed here, split stored)
    cost_usd: float
```

Additive, non-breaking extension of today's `(text, usage_dict)`. `usage_dict` stays available during migration (§4) so existing call sites keep working until cut over one at a time.

### 1.2 `llm.call()` — the invocation entry point

```
async def call(
    module_key: str,                 # NEW primary key (maps onto today's `stage`; see DQ-2)
    engagement_ctx: EngagementContext,
    *,
    template_vars: dict[str, Any],   # Jinja2 render vars (context, question, message, …)
    turn_id: str,                    # REQUIRED (== chat_turns.correlation_id). raises ValueError if None/empty — DEP-1
    max_tokens: int | None = None,   # None → ConfigManager default for module
    phi_detected: bool = False,      # forwarded to bandit eligibility filter (unchanged)
    experiment_axis: Literal["model", "prompt", "none"] = "none",  # DQ-6: set by RoundManager; structurally enforces §5 mutual exclusion
) -> LLMResponse
```

**`experiment_axis` (DQ-6 CLOSED — single per-turn field owned by RoundManager):**
- `"model"` → bandit explores model arms → PromptManager serves `default` (no A/B).
- `"prompt"` → A/B active → CallManager pins model to bandit's current-best (no model exploration).
- `"none"` → no experiment this turn → both normal.

One field, not two booleans — the mutual-exclusion rule (§5) is *structurally* enforced (the two experiment states are unrepresentable simultaneously) rather than runtime-asserted.

**`turn_id` required (DEP-1 CLOSED):** `call()` raises `ValueError` if `turn_id` is None or empty — a hard failure, not a warning. This makes the Eval **Attribution rule** structurally enforced at the call boundary rather than hoped for at the analytics layer: no row can be written without a `turn_id` to grade against. Forcing function, by design — see §8.1 for what this means for satellite callers.

`EngagementContext` is the tag source for prompt variant selection: **voice × format × autonomy × intent**. It is produced upstream (see §3 seam). CallManager does **not** invent it.

### 1.3 `EngagementContext` (input contract — **DQ-3 CLOSED** by Chat Architecture, owner of TurnProfile/EngagementContext)

Authoritative axis vocabularies (tag-match on **all 5**):

```
@dataclass(frozen=True)
class EngagementContext:
    voice:       str   # "clinical" | "operational" | "executive" | "general"
    format:      str   # "table" | "chart" | "bullets" | "prose" | "bars"
    autonomy:    str   # "agentic" | "copilot"
    intent:      str   # "explore" | "decide" | "retrieve" | "compare" | "act"
    caller_mode: str   # "real_time" | "background" | "batch"   ← 5th tag axis
```

`variant_tags` in `prompt_templates` carries all 5 axes; tag-match keys on all 5. Unknown/absent values fall through the fallback chain (§3.1) — never raise. **DQ-3 CLOSED 2026-07-24.**

> ⚠️ **caller_mode seed-time normalization requirement (Broadcaster-tracked):** the `caller_mode` axis has a documented latent bug — 3 incompatible caller-mode vocabularies exist across the codebase. The values above (`real_time | background | batch`) are **authoritative** for `variant_tags`. **The seed migration must normalize any existing `caller_mode` values to this vocabulary before tag-match can work on live data** (see §4 migration checklist). LLMManager does not solve the underlying vocab bug — Broadcaster tracks it; this is purely the seed-time normalization step so tag-match keys are stable.

---

## 2. DB schema

All three tables land as new migrations `049+` (`db/schema/NNN_*.sql`, applied by `app/db/run_migrations.py`). **DB gate: Platform-Architects ratify before land.**

### 2.1 `prompt_templates` (NEW)

```sql
CREATE TABLE IF NOT EXISTS prompt_templates (
    id            SERIAL PRIMARY KEY,
    module_key    TEXT    NOT NULL,          -- e.g. 'integrator', 'planner', 'react_1'
    role          TEXT    NOT NULL CHECK (role IN ('system','user')),  -- Option A: an LLM call = system + user pair
    variant_id    TEXT    NOT NULL DEFAULT 'default',
    variant_tags  JSONB   NOT NULL DEFAULT '{}',   -- {voice, format, autonomy, intent, caller_mode} (5 axes, DQ-3)
    version       INT     NOT NULL DEFAULT 1,
    template_body TEXT    NOT NULL,          -- Jinja2 source
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    weight        REAL    NOT NULL DEFAULT 1.0,     -- A/B sampling weight within a module_key
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (module_key, role, variant_id, version)
);
CREATE INDEX IF NOT EXISTS idx_prompt_templates_lookup
    ON prompt_templates (module_key, active);
-- GIN on variant_tags for the 5-axis tag-match JSONB lookups (§3.1). Reviewer-approved.
CREATE INDEX IF NOT EXISTS idx_prompt_templates_tags
    ON prompt_templates USING GIN (variant_tags);
```

- **Tag-match** by `variant_tags` (5 axes: voice/format/autonomy/intent/caller_mode) against `EngagementContext`, fallback chain **exact → partial → default** (§3.1).
- **A/B sampling** among active variants of a `module_key` by `weight`, keyed on `variant_id`.
- **Version** monotonic per `(module_key, variant_id)`; `active` selects the live row.

> **Product-truth gate (Product-Awareness Architect, signed 2026-07-24):** `variant_id` / `variant_tags` are **internal experiment-tracking only** — they flow to `llm_call_log` telemetry for quality audits and are **not customer-facing.** Verified: no user-facing envelope carries them (`AnswerCard` in `app/responder/final.py` has no variant field; the only user-visible "variant" strings — callout `info/warning/tip`, confidence badges — are unrelated UI styling). Only `active=true` templates execute (`active=false` = planned/archived — a clean reality gate). **Constraint (PA condition):** should prompt variants ever become *customer-facing* experiments, a corpus/product disclosure ("some features experimental, may change") must be added. Not the current design; recorded so the promise stays real if that changes.

### 2.2 `llm_configs` (NEW)

```sql
CREATE TABLE IF NOT EXISTS llm_configs (
    module_key     TEXT PRIMARY KEY,
    model_id       TEXT,               -- nullable: null → bandit/profile chooses (see DQ-4)
    temperature    REAL NOT NULL DEFAULT 0.1,
    max_tokens     INT  NOT NULL DEFAULT 1000,
    top_p          REAL,
    stop_sequences TEXT[],
    timeout_ms     INT  NOT NULL DEFAULT 30000,
    fallback_model TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

> **DQ-4 CLOSED (Eval):** Bandit owns model selection. `model_id` **null by default** = bandit chooses; non-null = **hard-pin escape hatch** (equivalent to a `model_profiles.yaml` entry). ConfigManager owns temperature/max_tokens/top_p/stop/timeout/**fallback** and never competes on model. Context-driven overrides are applied by ConfigManager **on top of** the row, keyed by `EngagementContext`. **Only `clinical voice → lower temperature` is implemented** (grounded in a ratified axis value).
>
> **Q3 CLOSED (Eval, Option 3) — temperature scheduling is RoundManager's, not EngagementContext's.** Rejected: `autonomy="agentic"` as the signal (autonomy = control-flow; temperature = sampling-variance — wrong proxy, same failure family as the caller_mode bug) and a new EngagementContext axis (temp varies **per-step intra-turn**; freezing it at turn level is wrong — EngagementContext is stable caller context, temp-schedule is execution). So `"thinking → higher temp"` stays an **unimplemented TODO** until RoundManager is specced and provides the signal. **Premise fix (correctness constraint, not preference):** "reasoning → higher temp" conflates two *opposite* objectives — **exploration/diversity** wants *higher* temp, **multi-step reasoning correctness** wants *lower* temp. Each module must pin which objective its temp modulation serves before a signal is assigned. **Eval cost note:** raising temp turns an otherwise-deterministic call non-deterministic → it moves from prefix-grade-once to grade-N-times, multiplying calibration cost — temp is not free w.r.t. calibration.
>
> **Precedent (Tech Health, for future — not a v1 condition):** `temperature` is the biggest reward confounder but not the only context-overridable sampled knob — `top_p` is too. If Eval ever finds residual unexplained arm-reward variance, **resolved `top_p` is the next column to log** by the identical argument (AC-21). Logged here so that decision cites this precedent instead of re-deriving it.
>
> **Mandatory guard — hard-pin excluded from model-arm training (selection-bias fix):** when `model_id IS NOT NULL`, the turn is a *forced* model choice — the bandit did not decide, so crediting/debiting that `(module_key, model)` arm is selection bias (same discipline as Router's `bypass_kind` on forced observations). CallManager sets `is_hard_pinned=True` on the `llm_call_log` row and passes the flag to the bandit reward path so it **skips the model-arm update**. Nuance: a hard-pinned-model turn **remains eligible for prompt-variant reward** if the prompt was the bandit's choice — the pin excludes the *model* axis from training, not the *prompt* axis.

### 2.3 `llm_call_log` — **see DQ-1.** Target column set (whether new table or added to `llm_calls`):

```
call_id (UUID PK) · turn_id (=correlation_id) · module_key · template_id · variant_id
· model_used · latency_ms · tokens_input · tokens_output · cost_usd
· is_hard_pinned (bool, DQ-4 — exclude from model-arm training) · experiment_axis ("model"|"prompt"|"none")
· temperature (REAL — Eval condition: resolved temp per call, so temp variance is not confounded into arm reward)
+ retained from llm_calls: provider, success, is_rate_limit, is_fallback, fallback_from,
  error_type, phi_detected, is_ab_call, ab_variant, config_sha, ts, synced_to_bq
```

**DQ-5 CLOSED:** keep the split — `llm_calls` already stores input/output separately (`tokens_input`, `tokens_output`); `LLMResponse.tokens_used` exposes the sum.

If we **extend** `llm_calls` (recommended): add `module_key`, `template_id`, `variant_id`, `turn_id`; keep `stage` populated in lockstep during migration (§4) so `022_model_performance_view` keeps working. If we build a **new** table: a compatibility view or a dual-write is required so the materialized view + bandit training don't go dark — that cost is why the recommendation is to extend.

### 2.4 DQ-1 ratification — Option A (extend), CONDITIONAL SIGN-OFF; authoritative corrected DDL

**Decision: Extend `llm_calls` (Option A).** Signed: Technical Health ✅ (conditional on defects 1–3), Eval-architect ✅ (conditional on BLOCKER + C1/C2a/C2b/C3a-c), Product-Awareness ✅ (unconditional). **Open: Database + UX lenses.**

**Ownership ruling (Technical Health):** **LLMManager owns `llm_calls` schema versioning going forward** (single gateway per Gate 1). Every schema change is gated through Database-architect ratification, because bandit training and Eval both read the table.

The DDL circulated in `docs/rag-agents/CHAT-DQ-1-DB-GATE.md` had **3 defects (reviewer-caught) + 2 deeper issues (found on my verification)**. Corrected, authoritative DDL below. *All claims verified against `020_llm_calls.sql` (has `provider`, `ab_variant`, `model`; NO `variant_id`, NO `module_key`) and `022_model_performance_view.sql` (`model_performance_by_stage` is a **MATERIALIZED VIEW**, `GROUP BY stage, model`, with dependent views `model_winner_by_stage` + `model_composite_scores`).*

**Migration order:** `prompt_templates` + `llm_configs` (§2.1/§2.2) first — before Migration A's `template_id REFERENCES prompt_templates(id)`.

**Migration A — additive columns + backfill (safe to land first):**
```sql
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS module_key     TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS variant_id     TEXT;          -- FIX (DEFECT 2 / Eval BLOCKER): was backfilled but never added
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS template_id    INT REFERENCES prompt_templates(id);
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS turn_id        TEXT;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS is_hard_pinned BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE llm_calls SET variant_id = 'default' WHERE variant_id IS NULL;       -- default inherits arm history (§4)
```

**A→B gate — EXECUTED, not assumed (Tech Health hardening + artifact-validation standard):**
```sql
SELECT COUNT(*) FROM llm_calls WHERE variant_id IS NULL;   -- MUST record 0 in the tracker before landing B
```

**Migration B — indexes + view (gated on the executed check above):**
```sql
-- FIX (DEFECT 1): arm-key index is NON-UNIQUE. llm_calls is a per-call LOG (one row per call);
-- a UNIQUE arm index fails the 2nd call on any arm. Uniqueness, if ever wanted, belongs on a rollup/arm-registry table.
-- FIX (finding #2): arm dimension is `model` (always populated), NOT legacy `ab_variant` (sparse/NULL for non-A/B).
-- Matches spec §5 arm key (model_id, module_key, variant_id).
CREATE INDEX IF NOT EXISTS idx_llm_calls_arm_key
    ON llm_calls (module_key, model, variant_id);

-- REQUIRED ADDITION (Tech Health): the exact Eval attribution join key — else every reward/eval query seq-scans.
CREATE INDEX IF NOT EXISTS idx_llm_calls_attribution
    ON llm_calls (turn_id, module_key);

-- FIX (DEFECT 3 + finding #1): model_performance_by_stage is a MATERIALIZED VIEW — `ALTER … ADD COLUMN` is invalid,
-- and CREATE OR REPLACE does NOT work on matviews. Must DROP CASCADE (drops dependents) then recreate all three,
-- adding variant_id to SELECT + GROUP BY. Refresh after recreate.
DROP MATERIALIZED VIEW IF EXISTS model_performance_by_stage CASCADE;  -- also drops model_winner_by_stage, model_composite_scores
-- … recreate model_performance_by_stage with `variant_id` in SELECT and `GROUP BY stage, model, variant_id`;
-- … recreate model_winner_by_stage + model_composite_scores (dependents; filtered to variant_id='default' to preserve admin behavior);
-- REFRESH MATERIALIZED VIEW model_performance_by_stage;
```

> **REQUIRED CO-CHANGE (implemented — migration 052, AC-20):** `model_registry.py:~1956` reads this view as `new_stats[stage][model]=dict(row)`. With `variant_id` in the grain, the read now filters `WHERE variant_id='default'` (the model-exploration slice per §5) so non-default variants can't overwrite per-`(stage,model)` model-selection stats. **052 must land in the same deploy as this read change** — migrations run at startup before the app serves, so within one deploy the ordering is safe.

**Build files (2026-07-25):** `db/schema/049_prompt_templates.sql`, `050_llm_configs.sql`, `051_llm_calls_llmmanager_cols.sql` (Migration A), `052_llm_perf_view_variant_and_arm_index.sql` (Migration B) + the `model_registry.py` read co-change. Routed to Technical Health for spot-check.

**`ab_variant` vs `variant_id` (Eval BLOCKER clarification — must be genuinely distinct):** `ab_variant` (existing) = the legacy **model**-A/B marker (`ab_variant = model_id`, populated only when `is_ab_call`). `variant_id` (new) = the **prompt-template** variant. Different axes. The canonical arm key uses `model` + `variant_id` (not `ab_variant`); `ab_variant` is retained for historical continuity only and is **not** an arm dimension going forward. → routed to Eval + Database for confirmation.

**Accepted reviewer conditions (carried into build):**
- **C1 (Eval):** bandit training must treat pre-migration `module_key IS NULL` history as a **separate cold-start bucket** — explicit filter/COALESCE, never silently merged into new `(module, variant)` arms.
- **C2a (Eval):** `is_hard_pinned` = **model**-selection-forced; gate **only the model arm**, not a blanket row-drop — a model-pinned/prompt-varying turn is still valid prompt-arm data (§5).
- **C2b (Eval):** `calibration_mode` turns MUST set `is_hard_pinned=TRUE` so the calibration freeze never leaks into prod bandit training.
- **C3a–c (Eval):** caller_mode normalization — coverage assertion **fail-closed** on any unmapped value (never silent-default to `real_time`); no-semantic-collapse check; historical key-remap (it's a bandit/eval conditioning key). See §4.
- **Ownership (Eval + Tech Health):** caller_mode old→new mapping is a **shared util owned by LLMManager**, applied in 3 places (seed, training-read remap, live legacy ingestion) — one source of truth, not three copies (three copies = the drift that created the 3-vocab bug). Unmapped-value behavior: documented default **+ warn log, never crash, never silently invent** (mirrors mobius-rag `DEFAULT_CALLER_MODE`).

---

## 3. Internal components — boundaries and interfaces

```
llm.call(module_key, engagement_ctx, …)
        │
        ├─▶ ConfigManager.get(module_key, engagement_ctx) ─▶ LLMConfig
        │        (llm_configs row + context overrides; 60s TTL cache; hot-reload)
        │
        ├─▶ PromptManager.render(module_key, engagement_ctx, template_vars, *, calibration_snapshot?, ab_allowed)
        │        (tag-match → fallback chain → A/B pick → Jinja2 render)
        │        ─▶ (rendered_prompt, template_id, variant_id)
        │
        └─▶ CallManager.invoke(config, rendered_prompt, meta) ─▶ LLMResponse
                 (bandit model select · retry ≤2 backoff · 429 queue+jitter
                  · model fallback · B2FMessage on fallback · one llm_call_log row)
```

### 3.1 PromptManager

- **Source of truth:** `prompt_templates`. Seeded from `config/prompts_llm.yaml` at version=1 (§4).
- **role pair (Option A):** `render()` selects a **system** template (tag-match / A-B — the experimental variant, e.g. integrator factual/canonical/blended) **and** a **user** template (role='user', deterministic default), Jinja-renders both, and returns the `(system_prompt, user_prompt)` pair. The tracked `(template_id, variant_id)` is the **system** (experimental) variant — that is the A/B arm dimension. A module may be system-only or user-only (the missing side renders `""`).
- **Tag-match / fallback chain:** `exact` (all 5 axes — voice/format/autonomy/intent/caller_mode — match `variant_tags`) → `partial` (subset match, most-specific wins) → `default` (`variant_id='default'`). Unknown/absent tag values never error — they fall through.
- **A/B sampling:** among `active` variants for the `module_key`, sample by `weight`, keyed on `variant_id`. **Suppressed when `ab_allowed=False`** (calibration snapshot active, or `bandit_exploring` — §5 rule 2).
- **Render:** Jinja2 over `template_body` with `template_vars`. (Migration note: existing templates use `{name}` `str.format` placeholders — the seed step converts `{x}` → `{{ x }}`; §4.) **Pin `autoescape=False` explicitly on PromptManager's Jinja2 `Environment` (Tech Health flag 2):** prompts are not HTML; an autoescape default would silently entity-escape quotes/`&`/`<` in some templates, a drift class the byte-identical parity test (AC-6) only catches for golden templates that happen to contain such characters.
- **`calibration_snapshot(module_key)`:** freezes the module's **`variant_id='default'`** template `(template_id, variant_id)` at batch start and **disables hot-reload** for that calibration run (calibration measures the baseline default-prompt under a controlled model; the measurement surface must not shift mid-run — ruling recorded 2026-07-25). Returns an opaque handle passed back into `call()` for every request in the batch.
- **Cache:** 60s TTL on the active-rows lookup for hot-reload.

### 3.2 ConfigManager

- **Source of truth:** `llm_configs`. Seeded from `config/prompts_llm.yaml:llm` + `config/model_profiles.yaml` (§4).
- **Context-driven overrides** applied over the base row, keyed by `EngagementContext` (thinking mode → higher temperature; clinical voice → lower). Override rules live in-module and are themselves versioned via `config_sha`.
- **Model selection stays delegated** to `model_registry` unless a row hard-pins `model_id` (DQ-4).
- **Cache:** same 60s TTL / hot-reload pattern.

### 3.3 CallManager

- Fetches config (ConfigManager) + prompt (PromptManager), calls the provider via the existing `model_registry` router path (`app/services/llm_manager.py:generate` logic is refactored here — retry, 429/TPD handling, EMA update, fallback are **already implemented** and are reused, not rewritten).
- **Retry:** max 2, exponential backoff. **429:** queue + retry with jitter (existing `tpd_tracker` reset-hint parsing reused). **Hard failure:** model fallback (`fallback_model` from config), **emit `B2FMessage` on fallback**.
- **Experiment axis:** reads `experiment_axis` and applies the §5 enforcement (default-prompt when `"model"`, model-pin-to-best when `"prompt"`).
- **Hard-pin:** when config `model_id IS NOT NULL`, sets `is_hard_pinned=True` and passes it to the bandit reward path so the model-arm update is skipped (variant-arm update still applies) — DQ-4.
- **Logging:** exactly one `llm_call_log` row per call (DQ-1), carrying `turn_id`, `module_key`, `template_id`, `variant_id`, `model_used`, `is_hard_pinned`, `experiment_axis`.

---

## 4. Migration path — hardcoded → version=1 seed rows

No behavior change on cutover. Sequence:

> **DQ-2 CLOSED — mandatory two-step migration guard (Eval verified against `022_model_performance_by_stage`).** The view does `GROUP BY stage, model` with a unique index on `(stage, model)`. Two distinct risks, two distinct migrations:
> - **Step A — `module_key` rename (free, no history loss):** renaming `stage`→`module_key` is a cosmetic no-op; all accumulated Beta arm history is preserved. Lands in the first migration.
>   - **✅ RESOLVED — Eval ruled 2026-07-25: Migration A DOES backfill `UPDATE llm_calls SET module_key = stage WHERE module_key IS NULL`.** C1 and DQ-2 provably don't collide because `module_key = stage` is a **rename (exact copy), not a coarsening**: (i) for 1:1 stages the copied label exactly equals the new arm → exact history carried (C1-compliant: exact, not coarse); (ii) for any stage that splits 1:many into multiple module_keys, the copied coarse `stage` value matches **no** fine module_key → it sits as an orphan legacy bucket and the new fine modules correctly **cold-start** (C1-compliant: coarse reward can't leak to a key it doesn't match). A false-merge would require assigning a coarse value *to* a fine key, which a literal copy never does. **C1's teeth are entirely on `variant_id`**, handled correctly (history=`'default'` exact; new/non-default variants cold-start per Step B). **Provenance:** do NOT overload `module_key IS NULL` for "pre-refactor row" (semantic double-load = future bug) — derive provenance from `created_at < migration_date` or the null `template_id`/`turn_id` that history lacks. **v1 note:** module_key ≡ stage is all-1:1 in v1, so "all history preserved" holds literally; 1:many stage splits are a future concern — confirm the split-stage list with the stage→module_key map owner before promising preserved history for any split module.
> - **Step B — `variant_id` split (SEPARATE migration, gated):** adding `variant_id` as an arm dimension shatters each `(stage, model)` arm into per-variant sub-arms. If it lands naively, **every arm resets to a zero prior the moment A/B launches — the bandit re-explores from scratch.** Guard: (1) **backfill** all existing `llm_calls` rows with `variant_id='default'`; (2) add `variant_id` to the view's `GROUP BY` **and** to the unique index — **only after** the backfill lands; (3) `default` **inherits** existing arm history, non-default (new A/B) variants start fresh. This migration is gated on the backfill completing first — it does **not** ride along with Step A.

1. **Land migrations 049+** (`prompt_templates`, `llm_configs`, `llm_call_log`/`llm_calls` extension incl. `is_hard_pinned`, `experiment_axis`). DB gate first. `module_key` rename (Step A) lands here; the `variant_id` arm-split (Step B) is a **later, separate** migration per the guard above.
2. **Seed `prompt_templates` @ version=1** from `config/prompts_llm.yaml:prompts.*` and the hardcoded prompts (`app/pipeline/react/prompts.py`, `app/stages/integrate.py`, `app/responder/final.py`). **Taxonomy — Option A (ruled 2026-07-25), `module_key ≡ stage` (DQ-2 is non-negotiable):**
   - **Single-prompt stages** (`planner`, `first_gen`, `rag_answering`, …): `module_key` = stage, `variant_id='default'`, `variant_tags='{}'`.
   - **Multi-prompt stages** use `variant_id` to differentiate — the yaml key is NOT the module_key. E.g. the **integrator** stage's 5 prompts → `module_key='integrator'`, with `integrator_system` = `variant_id='default'` and `integrator_factual/canonical/blended/repair` = **non-default** variants carrying `variant_tags` (e.g. `{"mode":"factual"}`) that drive tag-match selection at call time. Non-default variants cold-start (Step B) — correct, they are genuinely distinct prompts. Arm key `(model, integrator, factual)` vs `(model, integrator, default)` are distinct trackable arms; attribution join `(turn_id, module_key='integrator')` holds at the right granularity.
   - **Conversion (Finding A, verified):** system prompts are used **raw** today (never `.format()`'d) and the yaml has **zero `{{`/`}}` sequences**, so their literal JSON braces render byte-identical in Jinja with **no** conversion; only the `.format()`'d **user templates** convert `{var}`→`{{ var }}`.
   - **⚠️ BUILD FINDING (2026-07-25, caller_mode enumeration):** the legacy `caller_mode` field is overloaded across ≥3 *different semantic axes*, not one vocabulary with spelling drift: **timing** (`real_time`/`background`/`batch` — authoritative), **retrieval posture** (`research`/`custom_tight`/`canonical_first` — mobius-rag router), **namespaced** (`chat.default`/`chat.thinking` — mobius-rag Structure, `DEFAULT_CALLER_MODE="chat.default"`), **caller identity** (`auth_agent`). Mapping the non-timing values into `{real_time,background,batch}` would be the exact **C3b semantic-collapse**. Open for a ruling before the util maps them: (a) do any legacy values reach LLMManager's `EngagementContext.caller_mode` at all (RoundManager is the authoritative producer per DQ-3 — if it's the sole source, only timing values ever arrive and the fail-closed guard never fires in practice)? (b) which legacy values are genuine timing-synonyms (explicit map) vs must-**refuse** (C3a)? → Eval (C3b) + Chat Architecture (EngagementContext producer). Do NOT guess the non-timing mappings.
   - **caller_mode normalization (Eval C3a-c + ownership, §1.3):** old→new mapping is a **shared util owned by LLMManager**, applied in all 3 places it's needed (seed, historical training-read remap, live legacy ingestion) — one source of truth, never three copies (three copies = the drift that made 3 vocabs). Requirements: **C3a** coverage assertion, **fail-closed** — `SELECT DISTINCT caller_mode` over real data, every value has an explicit rule, **refuse to seed on any unmapped value** (never silent-default to `real_time`; unmapped → documented default **+ warn log, never crash**, per mobius-rag `DEFAULT_CALLER_MODE`). **C3b** no-semantic-collapse: where two old values map to one, confirm behavioral equivalence. **C3c** historical key-remap: caller_mode conditions bandit arms + calibration cells, so pre-normalization history must be remapped (or bucketed legacy) or one logical arm splits across two spellings.
3. **Seed `llm_configs` @ per-module** from `config/prompts_llm.yaml:llm` (temperature 0.1, model default) + `config/model_profiles.yaml` per-stage/fallback shape. `model_id` left null (bandit owns) unless a profile hard-pins.
4. **Cut over call sites one at a time**, each verified before the next (`react_1`, `react_2`, `planner`, `critique`, `integrator`, `integrator_roster`, `first_gen`, `repair`). Old `generate(prompt, stage)` path stays live until its last caller is migrated.
5. **Retire** the `prompts.*` block from `prompts_llm.yaml` only after all callers read from `prompt_templates`; keep `config_sha` semantics (now over the DB snapshot) for per-turn audit continuity.

**Seed-parity acceptance:** a rendered version=1 template must be **byte-identical** to the current `.format()` output for the same vars (golden-snapshot test per module_key). This is the guard against a silent regression on cutover.

---

## 5. Eval rules — VERBATIM, load-bearing

> **Attribution rule:** The `(turn_id, module_key)` join in `llm_call_log` is correct ONLY when grading is at module-output granularity. If any session grades at turn level and joins on `(turn_id, module_key)`, it silently smears one turn's score across every module's variant — phantom reward. Score per module output, or A/B one module at a time.

> **Mutual exclusion rule:** A/B and Model Bandit are mutually exclusive per turn. When `bandit_exploring` flag is set on the turn, PromptManager serves the default variant (no A/B). When A/B is active, model is pinned to bandit's current-best. CallManager enforces via the flag. Arm key = `(model_id, module_key, variant_id)`.

**Enforcement mechanics in this module — driven by the single `experiment_axis` field (DQ-6):**
- `experiment_axis="model"` → PromptManager invoked with `ab_allowed=False` → serves `variant_id='default'`; bandit explores model arms normally.
- `experiment_axis="prompt"` → A/B active: PromptManager samples among active variants; CallManager passes the bandit's **current-best** model to the router select path and suppresses model exploration for that call (model pinned to best).
- `experiment_axis="none"` → both normal, no experiment.
- Because the two experiment states live in one field, A/B and model-bandit exploration are **structurally** unable to co-occur on a turn — the mutual-exclusion rule is enforced by construction, not by a runtime assert.
- **Arm key `(model_id, module_key, variant_id)`** is the training-join key. Today's arms are keyed `(stage, model)`; `module_key`≡`stage` (DQ-2 CLOSED, rename is a no-op) and `variant_id` is added via the gated two-step migration where `default` inherits history (§4).
- **Hard-pin exclusion (DQ-4):** hard-pinned-model turns (`is_hard_pinned=True`) are excluded from the **model-arm** reward update but remain eligible for the **variant-arm** update. CallManager carries the flag to the reward path.
  - **C2a (Eval):** the exclusion is **per-axis** — `is_hard_pinned` means *the model selection was forced*; gate only the model arm, never blanket-drop the row (a model-pinned/prompt-varying turn is controlled, on-policy prompt-arm data).
  - **C2b (Eval):** `calibration_mode` turns (the §3.1 freeze) MUST set `is_hard_pinned=TRUE` so a calibration run never leaks into prod bandit training.
- **C1 (Eval) — arm cold-start:** pre-migration rows have `module_key IS NULL` (a single coarse legacy arm). Bandit training must treat them as a **separate cold-start bucket** (explicit filter/COALESCE), never silently merged into new `(module, variant)` arms — else a fine-grained arm inherits stale coarse reward and starts mis-primed.
- **Satellite confidence-tier guard (DQ-2 data caveat):** satellite-origin arms (`vibe`, `rag_strategy_*`, `lexicon_triage`) run out-of-process via `/internal/skill-llm` and are usually **not adjudicated** → `quality_score=NULL` / near-zero `quality_samples` (the known [satellite-telemetry gap]). The `022` view already encodes this as a **`low` confidence tier**. The bandit **must honor that tier** and must **not** treat a quality-blind satellite arm as comparable to a pipeline module with real adjudicated quality. This is a data-sufficiency guard the confidence tier already provides — the requirement is simply that it is **not overridden**. (No new mechanism; reused, §9.)
- **Forward constraint — temperature A/B (Eval, Q3):** if temperature ever becomes experimentable it becomes a **new `experiment_axis` value** obeying this same mutual-exclusion — never vary temperature AND model AND prompt in one turn (one axis moves, the others pin). Until then temperature is a resolved config value logged per call (AC-21), not an experiment axis.

---

## 6. Acceptance criteria (specific, testable)

**Contract**
1. `LLMResponse` returns all 8 fields populated on every successful call; `template_id`/`variant_id` reflect the row actually rendered.
2. `call(module_key, engagement_ctx, template_vars=…)` renders via Jinja2 and never requires the caller to pre-render a prompt string.

**PromptManager**
3. Exact-tag match returns the tagged variant; partial returns most-specific; no match returns `default`; unknown tag values never raise.
4. A/B: over N calls with two active variants weighted 50/50, observed split is within tolerance of 50/50, keyed on `variant_id`.
5. `calibration_snapshot(module_key)` freezes `(template_id, variant_id)` for the batch and hot-reload edits mid-batch do **not** change served template (guards the stale-import/mid-run failure mode).
6. **Seed parity:** version=1 render byte-identical to current `.format()` output for every migrated module_key (golden snapshots).

**ConfigManager**
7. `get(module_key, ctx)` returns the row with context overrides applied (thinking → higher temp, clinical → lower), verifiable per axis.
8. 60s TTL hot-reload: a row edit is reflected within TTL without redeploy; a snapshot in progress ignores it.

**CallManager**
9. Retry ≤2 with backoff on transient failure; 429 queues + retries with jitter; on hard failure falls back to `fallback_model` and **emits exactly one `B2FMessage`**.
10. **Exactly one** `llm_call_log` row per call, with correct `turn_id`(=correlation_id), `module_key`, `template_id`, `variant_id`, `model_used`, latency, tokens, cost.

**Eval rules**
11. `experiment_axis="model"` ⇒ served `variant_id='default'` (no A/B), asserted at the row.
12. `experiment_axis="prompt"` ⇒ model pinned to bandit current-best (no model exploration), asserted at the row. `"model"` and `"prompt"` are unrepresentable simultaneously (single field).
13. `022_model_performance_view` (or its successor) still populates after cutover — **no analytics regression** (guards DQ-1).
14. **Variant inherits history (DQ-2):** after the Step-B migration, the `default` variant's arm carries the pre-split `(module_key, model)` Beta counts; a freshly-added non-default variant starts from the zero prior. Asserted against the view.
15. **Hard-pin excluded from model-arm training (DQ-4):** a turn with `is_hard_pinned=True` produces **no** change to the `(module_key, model)` model-arm posterior; if its prompt was bandit-chosen, the variant-arm posterior **does** update. Asserted on the reward path.
16. **`turn_id` required (DEP-1):** `call(..., turn_id=None)` and `turn_id=""` both raise `ValueError` — asserted in a unit test. No `llm_call_log` row is ever written without a `turn_id`.
17. **Satellite confidence tier honored (DQ-2 caveat):** a quality-blind satellite arm (`quality_score=NULL`) is not ranked as comparable to an adjudicated pipeline module — the `low` tier is respected, not overridden.
18. **Migrations execute clean (DQ-1 §2.4):** Migration A adds `variant_id` before backfill; the A→B gate `SELECT COUNT(*) … WHERE variant_id IS NULL` returns **0** (recorded) before B lands; Migration B's arm-key index is **non-unique** and a second call on the same arm inserts fine; `model_performance_by_stage` and both dependent views exist and refresh after the DROP-CASCADE recreate.
19. **caller_mode mapping fail-closed (C3a):** the seed util **refuses** (does not default) on a caller_mode value with no explicit mapping rule; a coverage test over `SELECT DISTINCT caller_mode` passes only when every value is mapped.
20. **Perf-view grain coupled change (found at build, migration 052):** after `variant_id` enters the `model_performance_by_stage` grain, `model_registry`'s read (`:~1956`, `new_stats[stage][model]=dict(row)`) MUST filter `WHERE variant_id='default'` — else non-default variants overwrite per-`(stage,model)` stats and corrupt live model selection. Latent in v1 (all-default) → **must deploy the read change with migration 052.** Test: with a non-default variant present, model selection stats for `(stage,model)` are unaffected by the non-default row.
21. **temperature logged per call (Eval condition, Q3):** every `llm_call_log` row records the resolved `temperature`; CallManager writes it. Without it, temperature variance confounds arm reward (bandit can't separate "arm worse" from "arm hotter"). Test: a call at temp X logs `temperature=X`.
22. **calibration freezes temperature (Eval condition):** a calibration snapshot pins `(template_id, variant_id, temperature)` — not just the template. A config/hot-reload edit mid-batch does not change the temperature served under an active snapshot (the measured instrument stays fixed).

**Health gate:** Technical Health RED on any of the above = P0 ship-blocker.

---

## 7. Seams (producer ↔ consumer — confirm DIRECT with each owner)

| Seam | Counterparty | What must be agreed | Status |
|---|---|---|---|
| **EngagementContext producer** | Chat Architecture (owns TurnProfile/EngagementContext) | dataclass shape + 5 axis vocabularies (DQ-3) | ✅ CLOSED (DQ-3) |
| **ClientChannel / B2F** | ClientChannel (parallel Phase-1 session — may not exist at build time) | CallManager emits `B2FMessage` on fallback (§3.3, AC-9). **Build-order (Tech Health flag 1):** use the existing emit path as a shim until ClientChannel lands, then migrate the emit call — so AC-9 stays testable and this doesn't block on a sibling session. | OPEN — shim at build, migrate later |
| **`turn_id` = `correlation_id`** | Chat worker / persistence | LLMManager OWNS the not-null invariant (`call()` raises on null `turn_id`, DEP-1). Satellite propagation gap out of scope — Broadcaster tracks; satellites must propagate before migrating onto `llm.call()`. | ✅ CLOSED (DEP-1) |
| **Satellite-origin `stage` values** | Eval | 1:1 rename covers satellite arms too (Option B); they stay first-class arms, quality-blind → `low` tier honored (§5). | ✅ CLOSED (DQ-2) |
| **Model Bandit** | Eval (verified against `022` view) | arm key gains `variant_id` (gated migration, default inherits); hard-pin excluded from model-arm training; `experiment_axis` drives A/B-vs-model exclusion; `module_key`≡`stage` | ✅ CLOSED (DQ-2/4/6) |
| **`llm_call_log` schema** | Platform-Architects (DB gate) + Eval (reads for grading) | DQ-1 table decision (extend `llm_calls`); corrected DDL §2.4 | ✅ CLOSED 5/5 (DQ-1) |
| **Existing analytics consumers** | `022_model_performance_view`, bandit training reads | no-orphan guarantee on cutover (AC-13) | must not regress |

---

## 8. Open DESIGN questions — need rulings BEFORE build (per process gate)

- **DQ-1 (5-architect DB gate) — CONDITIONALLY SIGNED (3 of 5):** Extend `llm_calls` (Option A). ✅ Technical Health (cond. defects 1–3), ✅ Eval-architect (cond. BLOCKER + C1/C2a/C2b/C3a-c), ✅ Product-Awareness (uncond.). **OPEN: Database + UX lenses.** Corrected authoritative DDL in **§2.4** — 3 reviewer defects + 2 of my own findings (materialized-view DROP+CASCADE recreate; arm key on `model` not sparse `ab_variant`) fixed. Conditions carried into §2.4/§4/§5.
- **DQ-2 (Eval) — ✅ CLOSED 2026-07-24 (Option B, verified against full view):** `module_key` ≡ `stage` 1:1 rename for **ALL callers — pipeline and satellite alike**. No namespace prefix, no migration for satellite rows. `022_model_performance_by_stage` is `FROM llm_calls … GROUP BY stage, model` with **no pipeline-only filter** — satellite stages (`vibe`, `rag_strategy_*`, `lexicon_triage`) are already first-class arms; a 1:1 rename preserves all their history (Option A/prefix would orphan it, same failure as the variant split). `variant_id` arm-split remains a **separate, gated** migration where `default` inherits history (§4). **Data caveat:** satellite arms are quality-blind (`quality_score=NULL`) — see §5 confidence-tier guard.
- **DQ-3 (EngagementContext owner) — ✅ CLOSED 2026-07-24:** 5 axes ratified (§1.3).
- **DQ-4 (Eval) — ✅ CLOSED 2026-07-24:** bandit owns model; `model_id` null default, non-null = hard-pin; hard-pinned turns excluded from model-arm training (`is_hard_pinned`), still eligible for variant-arm reward.
- **DQ-5 (Eval/Coordinator) — ✅ CLOSED 2026-07-24:** keep split (`tokens_input`/`tokens_output`), expose sum in `LLMResponse.tokens_used`.
- **DQ-6 (Eval) — ✅ CLOSED 2026-07-24:** single `experiment_axis` field ("model"|"prompt"|"none") set by RoundManager, passed as `call()` arg — mutual exclusion enforced structurally.

**Remaining blocker before build: DQ-1 (DB gate, Broadcast/Platform-Architects) + DEP-1 ruling (§8.1).** Open **BUILD** questions (implementation of a known thing) are fine to defer.

### 8.1 New dependency surfaced during verification (turn_id seam is currently BROKEN)

Verifying `SPEC_LLM_CALLS_CORRELATION_ID_PROPAGATION.md` (per coordinator request) confirmed it is **consistent** with §7 on the core contract (turn_id == `correlation_id`, single writer at `llm_manager.py:222`) — but it documents that the seam is **currently broken in production**: **76% of `llm_calls` rows have NULL `correlation_id` and 100% have NULL `thread_id`**, because satellite services (vibe, instant-rag, rag_strategy_*, lexicon_triage) call LLMs via `/internal/skill-llm` without propagating the ids.

**Impact on the Attribution rule (§5):** the `(turn_id, module_key)` grading join is only as good as `turn_id` population. If `llm_call_log.turn_id` inherits this gap, **per-module grading silently drops every satellite-origin call.**

**DEP-1 CLOSED (Chat Architecture) — LLMManager owns the not-null invariant.** `call()` requires non-null `turn_id` and raises `ValueError` if absent (§1.2). This makes the Attribution rule structurally enforced by construction rather than hoped for at the analytics layer.
- **What this does NOT mean:** LLMManager does **not** fix the satellite propagation gap. Satellites calling `/internal/skill-llm` without `correlation_id` are **out of scope** for this spec — separate work, **Broadcaster tracks**.
- **What it DOES mean (deliberate forcing function):** once LLMManager rolls out, any caller that cannot supply `turn_id` **cannot use `llm.call()`** until it fixes its own propagation. Satellite callers must propagate `correlation_id` end-to-end (per `SPEC_LLM_CALLS_CORRELATION_ID_PROPAGATION.md` Fix A/B) before they can migrate onto `llm.call()`. That's the point, not a bug.

*(Minor: that draft proposes a migration numbered `035`, which now collides with the existing `035_user_tool_subscriptions.sql`. Their doc's problem, noted for whoever picks it up.)*

---

## 9. What is NOT changing (reuse, don't rewrite)

- Bandit selection, EMA update, TPD/429 reset-hint parsing, circuit breaker, `record_call_failure` — reused from `model_registry` + current `llm_manager.generate`.
- **Confidence-tier weighting for quality-blind satellite arms** — already provided by the `022` view's tier; LLMManager adds no mechanism, only the requirement that it not be overridden (§5, DQ-2 caveat).
- `config_sha` per-turn audit — retained, now computed over the DB snapshot.
- `correlation_id` propagation — follow existing `SPEC_LLM_CALLS_CORRELATION_ID_PROPAGATION.md`.

---

## 10. Error contracts (Gate 2)

The exception a caller sees is part of the public contract. Typed in
`app/services/llm_manager_errors.py`; each subclasses the builtin it logically
is, so existing `except ValueError`/`except LookupError` and tests keep working.

| Raised by | Type (base) | When | Caller expectation |
|---|---|---|---|
| `call()` | `TurnIdRequiredError` (`ValueError`, `LLMManagerError`) | `turn_id` None/empty (DEP-1) | Programmer error — fix the caller; never retried. Enforces the Attribution rule at the boundary. |
| `PromptManager.render` / `default_template` | `PromptNotFoundError` (`LookupError`, `LLMManagerError`) | no active template / no matched variant / no `default` for the module | Config/seed error — surface, don't retry. Indicates the module was not seeded or `active=false`. |
| `CallManager` invoke layer | `LLMCallError` (`LLMManagerError`) | provider failed **terminally** — after retry (≤2, backoff), 429 queue+jitter, and model fallback all exhausted | Transient-exhausted; carries `__cause__`. A `B2FMessage` was already emitted on the fallback attempt (§3.3). |
| `ConfigManager.get` | *(no raise)* | missing `llm_configs` row | Falls back to `_default_config` (bandit owns model); a module runs unconfigured rather than failing. |

**Handled internally (do NOT propagate):** provider 429 (queued + retried with jitter), single transient provider error (retried ≤2 with backoff), primary-model hard failure (falls back to `fallback_model`, emits B2F). Only after **all** of these are exhausted does `LLMCallError` surface.

**Acceptance:** AC-16 (`TurnIdRequiredError` on empty turn_id) · AC (PromptNotFoundError on unseeded module) · the retry/429/fallback/B2F path is AC-9. `LLMCallError` is raised only post-exhaustion (asserted once the production `invoke_fn` is wired — gated on migration 051 landing).

---

*Routed to Chat Architecture for review. On approval, Chat Architecture routes to Technical Health for sign-off. DB migrations additionally gate on Platform-Architects; the bandit-seam DQs gate on the Model Bandit owner. No code until Technical Health sign-off.*
