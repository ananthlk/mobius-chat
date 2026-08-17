# Batch Deploy Coordination — #107 (2026-08-16)

## Status: READY TO DEPLOY — #107 wall-clock guard

**Commit:** `d671d7b` — `app/pipeline/react_loop.py` lines 4876–4893  
**Change:** Wall-clock guard in critic extension gate. Before granting each extension, checks `_cc_elapsed_s + 25 < _cc_deadline_s`. If not enough headroom, skips extension and falls through to synthesis. Existing `max_extension_rounds` check untouched.

## Instructions for LLM Agent: DEPLOY NOW

```bash
cd /Users/ananth/Mobius/mobius-chat
npm run build
scripts/deploy.sh dev
```

Record deployed revision below after deploy completes.

**Deployed revision:** _pending_

---

# Previous batch — #95/#104/#105/#106 (2026-08-16)

Written by Chat Master as durable coordination channel (direct session messages unreliable).

## Status: ALL CODE COMMITTED — READY TO DEPLOY

Verified via `git log` and `grep` on committed files:

| # | Feature | Committed at | Symbol verified |
|---|---------|-------------|-----------------|
| #103 | RAG ceiling 6 + integrator 16384 | `b4e09da` | `_rag_call_ceiling_for_mode` in react_loop.py ✅ |
| #104 | Critic completion gate hook | `b4e09da` | `react_completion_critic` call at react_loop.py:4857 ✅ |
| #104 | Critic module + context fields | `de1cf24` | `build_completion_critic_user_message` in critic.py ✅ |
| #104 | Stage registration | `bf7eecd` | in model_registry.py ✅ |
| #105 | curated_chunk_ids write-site | `b4e09da` | react_loop.py:4695-4698 ✅ |
| #105 | _build_rag_chunks curated path | `552b561` | integrate.py ✅ |
| #95 | gaps_closed fallback synthesis | `b4e09da` | react_loop.py:3604-3657 ✅ |
| #106 | Report-mode formatting | `590e7fd` | `_report_mode_instructions` in final_parallel.py ✅ |

## Remaining uncommitted (non-blocking)

- `frontend/src/app.ts` / `frontend/static/app.js` — 6 changed lines, Chat FE agent owns these. Confirm before staging.
- Several untracked doc files (`docs/CHAT-REFACTOR-PROCESS.md`, etc.) — not needed for this deploy.

## Instructions for LLM Agent: DEPLOY NOW

All backend changes are committed to `main`. Run the deploy:

```bash
cd /Users/ananth/Mobius/mobius-chat
npm run build
scripts/deploy.sh dev
```

(Do NOT use `gcloud run deploy --source .` — see `feedback_chat_deploy_cache_bug.md`)

After deploy completes, record the new revision here:

**Deployed revision:** mobius-chat-00866-h2k (image tag 20260817-022953-7d51b538bc), deployed 2026-08-17 by LLM Agent, serving 100% traffic. Post-deploy smoke: 5/5 pass.

Then run live verification tests (see below) and record results.

## Live verification tests (Chat Master owns these)

Post-deploy, Chat Master will run:

1. **#95**: Fast-path query → check `gaps_closed` in diagnostics → follow-up turn to verify cross-turn facts flow
2. **#104**: Multi-category FL Medicaid query in think mode → confirm extra rounds fired for uncovered categories
3. **#105**: 4-payer comparison query → verify all payers appear in integrator output (corpus caveat: Humana FL Medicaid may still be thin)
4. **#106**: Multi-service think-mode query → verify `##` headers, comparison table, no bullet dump

## Humana corpus gap (flag to Sourcing — separate)

Humana FL Medicaid behavioral-health docs poorly indexed. Need:
- AHCA Coverage and Limitations Handbooks: 59G-4.100, 59G-4.087, 59G-4.199
- Humana FL Medicaid behavioral health docs

This is a corpus issue, NOT a code issue. #105's curated chunk logic works — the test was limited by missing docs.
