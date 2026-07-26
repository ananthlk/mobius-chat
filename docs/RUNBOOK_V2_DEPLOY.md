# Runbook — deploy v2 modular-prompt enrichment to GCP dev (+ live test)

**Status:** STAGED. Every step below is push-button EXCEPT step 0, which gates all of it.
**Blocker (step 0):** Database hands over the Q3/Q4 trigger DDL → migration 053 lands. Until then, the block tables don't exist and steps 2–6 cannot run.
**Owner:** LLMManager (steps 0–4, 6) · Chat front end (step 5).

---

## The chain (why each step gates the next)

```
0. 053 lands (Database triggers)         ← BLOCKER. tables + immutability triggers exist.
1. apply 053 to dev DB                   ← prompt_blocks / prompt_compositions / members / snapshots
2. seed blocks + compositions            ← block_seed.py (idempotent upsert). the v2 prompts become rows.
3. wire PromptManager.resolve_composition← integrate path reads a composition → BlockAssembler.assemble
4. deploy mobius-chat to GCP dev         ← scripts/deploy.sh dev  (NOT `gcloud run deploy --source .`)
5. merge + deploy FE now-slice           ← Chat front end's claude/chat-fe-v2-answercard
6. flip the flag on dev                  ← MOBIUS_INTEGRATOR_MODE=parallel  (canary first, then %)
```

Nothing before step 3 changes live behavior — it only stages data + code. The flag flip (step 6) is the single switch that puts modular enrichment on the live path, and it is instantly reversible (`=sequential`).

---

## Steps

### 0. GATE — 053 lands
Database delivers `tg_authority_immutable_kind` (Q3) + `tg_block_body_immutable` (Q4) DDL → paste into `db/schema/053_prompt_block_persistence.sql` §5 → migration complete. **053 must NOT land with the trigger section `[PENDING PASTE]`** (Tech Health landing gate — the snapshot manifest→blocks dereference depends on body immutability).

### 1. Apply 053 to dev
```bash
# from mobius-chat/, against the dev DB (cloud-sql-proxy on :5433 — restart it first if it's been up a while)
psql "$MOBIUS_DEV_DB_URL" -f db/schema/053_prompt_block_persistence.sql
```
Verify: `\dt prompt_*` shows the four tables; `\d prompt_blocks` shows `ck_authority_validated` + the two triggers.

### 2. Seed blocks + compositions
```bash
python3 -m app.services.block_seed --env dev            # idempotent: ON CONFLICT DO NOTHING
```
Seeds the validated v2 blocks (enricher module first) + one `active` composition per integrator module. Re-runnable safely. Verify: `SELECT module_key, status, active FROM prompt_compositions;` shows an active row per module.

### 3. Wire the read path
`PromptManager.resolve_composition(module_key, ctx)` → active composition → members (resolve `pinned_version` NULL → latest active block) → `BlockAssembler.assemble(...)`. Gate behind `MOBIUS_PROMPT_SOURCE=composition|template` so it falls back to the 049 template path per-module (coexistence, spec §5). **Unit-tested against the seeded dev DB before deploy.**

### 4. Deploy to GCP dev
```bash
scripts/deploy.sh dev      # monorepo-aware; `gcloud run deploy --source .` FAILS here (memory: mobius-chat deploy method)
```
Check traffic pins to the new revision (RAG-deploy gotcha: traffic can stay on the old rev).

### 5. FE now-slice (Chat front end)
Merge `claude/chat-fe-v2-answercard` (card-render-model + tab bar), `npm run build` in `frontend/`, deploy. Backward-compatible: legacy + v2 cards both render at any rollout %.

### 6. Flip the flag (the live test)
```bash
# canary: force parallel for one’s own turns first
MOBIUS_INTEGRATOR_MODE=parallel      # forces the modular path
# or ramp: MOBIUS_INTEGRATOR_PARALLEL_PCT=5 → 25 → 100
```
Then run real chat turns on dev. **Instant rollback:** `=sequential`.

---

## What "live test" proves at step 6
- The parallel A/B/C enricher runs on **composed modular prompts** (not the hardcoded `chat_config` strings).
- Every call logs `composition_hash` → dereferenceable to the exact block manifest (053 snapshot registry) for attribution/ablation.
- The FE renders the v2 card (mode-optional, per-section visibility, envelope formats, tab layout).

## Pre-053 live signal (available NOW, no GCP)
`scratchpad/v2_migrate_enricher.py` runs the composed v2 enricher prompt against real Claude, before/after vs the monolith. Latest run: **v2 judged BETTER** — same corrections + source fidelity, cleaner sections, fewer redundant citations, valid JSON. This tests enrichment quality without the GCP path or UI.
