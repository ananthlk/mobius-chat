# Runbook — deploy v2 modular-prompt enrichment to GCP dev (+ live test)

**Status:** ✅ **COMPLETE — LIVE ON DEV** (2026-07-26). All 6 steps executed. Serving revision `mobius-chat-00592-8r7`, `MOBIUS_PROMPT_SOURCE=composition`, verified via a real production turn (log: `[integrator] v2 composition prompt module=integrator_enricher_blended hash=c0c6f950690db74b`) and a live screenshot of the v2 tabbed-bubble UI (Summary/Citations/Corrections/Follow-up/Tasks/Diagnostics) rendering it correctly, including gaps-routing and a real correction firing.
**Owner:** LLMManager (steps 0–4, 6) · Chat front end (step 5, banked green ahead of need).

## §0 — Four real bugs found and fixed during rollout (read before your next deploy)

Getting from "flag is set" to "actually running" took four independent fixes, each caught by verifying rather than assuming a step worked:

1. **Log-query mistake (mine).** `mobius-chat` logs structured JSON — the message is under `jsonPayload.message`, not `textPayload`. Early verification attempts silently matched nothing and looked like the pipeline was stuck. **Always query `jsonPayload.message` on this service.**
2. **Prompt-mode coverage gap.** The initial cutover only wired the `factual` consolidator type; live dev traffic defaults to a canonical score of 0.50 → `blended` mode, which never touched the composed prompt. Fixed by generalizing the block decomposition to cover any mode and seeding `integrator_enricher_blended` too (§ below). **Any future mode/consolidator-type addition needs its own composition seeded — nothing falls back to "compose it anyway."**
3. **`deploy.sh`'s `SET_ENV_VARS` is a hardcoded allowlist array, separate from `deploy/<env>.env`.** Adding a var to `dev.env` is necessary but NOT sufficient — it must ALSO be added as an explicit line in `scripts/deploy.sh`'s `SET_ENV_VARS` array, or it's silently absent from every deploy no matter what's in the env file. (This predates v2 — even `MOBIUS_INTEGRATOR_PARALLEL_PCT` had this exposure.) **Fixed** — `MOBIUS_PROMPT_SOURCE` is now in both places.
4. **DB password injection.** Cloud Run's `CHAT_RAG_DATABASE_URL` carries no password (unix-socket DSN); the password must be regex-injected from the `CHAT_DB_PASSWORD` secret, exactly as `app/db_client._get_fallback_url` already does for the rest of the app. My first sync-resolver implementation used a different (broken) approach and silently fail-soft'ed to the old hardcoded prompt with no error visible to a user. **Fixed** — `prompt_manager._connect_sync` now calls `_get_fallback_url` directly instead of re-deriving the same logic.

None of these broke a live user turn — the fail-soft design degraded to the pre-v2 prompt each time, which is why they were only caught by explicit log verification, not by a turn failing.

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
