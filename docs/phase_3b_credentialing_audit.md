# Phase 3b audit — the 41 chat-side credentialing endpoints

**Date:** 2026-04-17
**Input:** `app/api/credentialing.py` (15 endpoints) + `app/api/roster.py` (26 endpoints)
**Goal:** classify each endpoint so Phase 3c / 3d / 3e become concrete work
items, not hand-waved "remove the proxies."

## Headline

| Category | Count | What it means | Fix shape |
|:--:|:--:|---|---|
| **A — Pure proxy** | **25** | Just forwards to the credentialing skill server; no chat-side DB or state writes | **Delete from chat** once FE can call skill directly |
| **B — Mixed** | **4** | Proxies to skill AND writes to chat-side DB (cascades, task-manager mirrors, AI summary persistence) | **Migrate side-effects to skill** (chat becomes thin glue or gone) |
| **C — Chat-only** | **12** | No skill call at all — these orchestrate credentialing RUNS using chat's PG tables (`credentialing_runs`, `roster_truth`, `roster_snoozes`) | **Relocate to the credentialing skill** — the *state* these touch belongs with the skill, not chat |

**Blunt takeaway:** **61% (25/41) of these endpoints can be deleted from
chat in a single pass** if the FE swaps its base URL. The other 39% needs
structural work because chat-side state escaped into chat's DB.

## Category A — Pure proxy (25 endpoints)

Every one of these looks like:

```python
@router.get("/chat/roster-reconcile/...")
def handler(...):
    base = _skill_base()  # CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL
    if not base:
        raise HTTPException(503, ...)
    with httpx.Client(timeout=15.0) as c:
        r = c.get/post/patch(f"{base}/roster/...", ...)
        return r.json()
```

No DB calls. No chat-state writes. No business logic — just URL translation
and timeout/error wrapping.

### The 25 pure proxies

| Method | URL | Fn | Notes |
|---|---|---|---|
| GET | `/chat/npi-lookup/{npi}` | `npi_lookup` | NPPES registry call (not even the credentialing skill — direct to CMS) |
| GET | `/chat/roster-org/{org_name}/dismissals` | `roster_org_dismissals_proxy` |  |
| GET | `/chat/roster-reconcile/latest-for-org` | `roster_latest_for_org` |  |
| GET | `/chat/roster-reconcile/lookup-npi` | `roster_lookup_npi` | Also calls NPPES |
| GET | `/chat/roster-reconcile/npi-search` | `roster_npi_search_proxy` |  |
| PATCH | `/chat/roster-reconcile/provider/{provider_id}` | `roster_provider_save_decision` |  |
| DELETE | `/chat/roster-reconcile/provider/{provider_id}` | `roster_provider_delete` |  |
| POST | `/chat/roster-reconcile/provider/{provider_id}/approve` | `roster_provider_approve` |  |
| POST | `/chat/roster-reconcile/provider/{provider_id}/audit-log` | `roster_write_audit_proxy` |  |
| GET | `/chat/roster-reconcile/provider/{provider_id}/audit-log` | `roster_read_provider_audit_proxy` |  |
| POST | `/chat/roster-reconcile/provider/{provider_id}/revalidate` | `roster_provider_revalidate` |  |
| GET | `/chat/roster-reconcile/run/{run_id}/audit-log` | `roster_read_run_audit_proxy` |  |
| GET | `/chat/roster-reconcile/search-nppes` | `roster_search_nppes` |  |
| GET | `/chat/roster-reconcile/uploads` | `roster_reconcile_uploads_for_org` |  |
| POST | `/chat/roster-reconcile/{upload_id}/llm-clean` | `roster_llm_clean` | 177 lines — biggest proxy, but still pure passthrough |
| GET | `/chat/roster-reconcile/{upload_id}/llm-clean-cache` | `roster_llm_clean_cache_proxy` |  |
| POST | `/chat/roster-reconcile/{upload_id}/mass-approve` | `roster_mass_approve_proxy` |  |
| GET | `/chat/roster-reconcile/{upload_id}/progress` | `roster_reconcile_progress_proxy` | SSE — may need gateway support for streaming |
| GET | `/chat/roster-reconcile/{upload_id}/report` | `roster_reconcile_report_proxy` |  |
| GET | `/chat/roster-reconcile/{upload_id}/status` | `roster_reconcile_status_proxy` |  |
| GET | `/chat/roster-truth/{org_name}` | `roster_truth_proxy` |  |
| GET | `/chat/roster-truth/{org_name}/org-summary` | `roster_org_summary_proxy` |  |
| POST | `/chat/roster-truth/{org_name}/provider` | `roster_provider_add_proxy` |  |
| GET | `/chat/roster-truth/{org_name}/provider/{provider_id}` | `roster_provider_detail_proxy` |  |
| PATCH | `/chat/roster-truth/{org_name}/provider/{provider_id}` | `roster_provider_edit_proxy` |  |

### Phase 3c — delete the 25 pure proxies

Three sub-steps:

1. **Skill server auth + CORS.** The skill server at
   `CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL` needs the same
   JWT / CORS guards chat currently applies on these routes. If the skill
   already validates JWT (check `mobius-skills/provider-roster/...`),
   we're good. If not, that's prep work.
2. **FE URL swap.** Frontend calls switch base from
   `${API_BASE}/chat/roster-reconcile/...` to the skill public URL.
   Delivered via a new `SKILL_BASE_URL` env injected into the FE bundle.
3. **Chat deletes the endpoints.** Once FE is cut over, remove these 25
   handlers. Chat's router files shrink dramatically.

Estimated effort: **0.5 day** (assuming skill auth/CORS is already sane).

## Category B — Mixed (4 endpoints)

Each proxies to the skill AND writes chat-side state or mirrors into
another skill (task-manager). These need the chat-side work moved into
the skill, or the FE to make two calls (one to skill, one to task-manager)
directly.

| Method | URL | Chat-side thing it does |
|---|---|---|
| DELETE | `/chat/credentialing-runs/{run_id}` | Cascades: calls skill's `DELETE /roster/reconcile/{upload_id}` AND deletes chat's `credentialing_runs` row |
| PATCH | `/chat/credentialing-runs/{run_id}/pml-tasks` | Writes chat `credentialing_runs` state AND mirrors resolved/dismissed into task-manager (a second skill) |
| PATCH | `/chat/credentialing-runs/{run_id}/taxonomy-tasks` | Same pattern as pml-tasks |
| POST | `/chat/roster-truth/{org_name}/provider/{provider_id}/summary` | Calls LLM for AI summary + persists to chat's `roster_truth_pg.upsert_ai_summary` |

### Phase 3d — three migration patterns to choose from

For each mixed endpoint, one of:

- **(i) Move chat-side DB write to the skill.** Skill owns the full state;
  chat side becomes a pure proxy and joins Category A.
  Requires: credentialing skill learns to touch what are today chat tables.
  Cleanest long-term; biggest migration.

- **(ii) FE makes two calls.** FE calls skill, then separately calls
  task-manager (or an event bus). Eliminates chat as middleman for the
  task-manager mirror.
  Cleanest short-term; pushes coordination onto FE.

- **(iii) Keep chat as a coordinator.** Accept that a few endpoints stay
  in chat because they orchestrate *across* skills (credentialing ↔
  task-manager). This is the pragmatic option and probably right for the
  pml-tasks / taxonomy-tasks pair.

Recommended mix: **(iii) for `pml-tasks` + `taxonomy-tasks`**, **(i) for
`summary`** (clearly belongs in credentialing), **(i) for `DELETE run`**.

Estimated effort: **1 day**.

## Category C — Chat-only (12 endpoints)

These don't call the skill at all — they orchestrate credentialing run
*state* using chat's PG tables: `credentialing_runs`, `roster_truth`,
`roster_snoozes`, and the in-memory `_store_*` registry in
`app.services.credentialing_run_service`.

| Method | URL | Touches |
|---|---|---|
| GET | `/chat/credentialing-runs` | `credentialing_runs_pg.list_credentialing_runs` |
| POST | `/chat/credentialing-runs` | `credentialing_run_service.create_credentialing_run` + `threads.save_state` |
| GET | `/chat/credentialing-runs/{run_id}` | `get_credentialing_run` |
| GET | `/chat/credentialing-runs/{run_id}/org-npis` | run state + `credentialing_assertions_pg` + NPPES |
| GET | `/chat/credentialing-runs/{run_id}/roster-diff` | roster truth diff logic |
| POST | `/chat/credentialing-runs/{run_id}/roster-snooze` | roster truth mismatch snooze |
| GET | `/chat/credentialing-runs/{run_id}/roster-snoozes` | list snoozes |
| GET | `/chat/credentialing-runs/{run_id}/roster-truth` | get truth |
| POST | `/chat/credentialing-runs/{run_id}/roster-truth` | upsert truth |
| POST | `/chat/credentialing-runs/{run_id}/seed-roster` | patch run state |
| POST | `/chat/credentialing-runs/{run_id}/validate` | orchestrator advance |
| DELETE | `/chat/roster-truth` | dev-only clear |

**These represent the credentialing state model living in chat's
database.** That's the root architectural mismatch the user flagged with
"credentialing is a skill, not a chat interface."

### Phase 3e — relocate state to the credentialing skill

Two paths:

- **Path 1 (clean):** Move these endpoints + their storage (`credentialing_runs_pg`, `roster_truth_pg`, `credentialing_run_service`) into the skill package. Chat no longer knows about credentialing state. Biggest refactor — probably **3–5 days** and couples to the credentialing-server deployment changes.

- **Path 2 (pragmatic):** Keep credentialing state in chat's DB for now but expose it via a *credentialing HTTP surface* that both the FE and chat's ReAct tools call. No more `/chat/credentialing-runs/*` paths — they become `/credentialing/runs/*` on the same chat process but under a distinct router not wired to `/chat/*`. This is ~0.5 day; signals intent without full extraction.

Recommended: **Path 2 for the immediate refactor**, Path 1 on a deliberate
timeline once the skill service is ready to own the DB.

## Category D — reconsider

The audit flagged 1 endpoint (`/chat/npi-lookup/{npi}`) as "unclear"
because my regex missed that the `urllib` call is routed through a helper
(`_fetch_nppes_single`). Reclassified as **A — pure proxy** (hits the
NPPES registry directly, no chat state). The table above already reflects
this.

## Recommended execution order

1. **Phase 3c (0.5 day)** — delete the 25 pure proxies. Biggest LOC win,
   smallest risk.
2. **Phase 3d (1 day)** — handle the 4 mixed endpoints per pattern mix
   above (ii-for-pml-tasks, i-for-summary and delete-run).
3. **Phase 3e path 2 (0.5 day)** — rename the 12 chat-only endpoints from
   `/chat/credentialing-runs/*` to `/credentialing/runs/*` under a new
   `app/api/_credentialing_host.py` router that is NOT part of chat's
   `/chat/*` surface. This is the "chat doesn't expose credentialing to
   the FE" move without the full DB extraction.
4. **Phase 3e path 1 (3–5 days, later)** — full extraction, chat stops
   owning any credentialing state.

Blocker for 3c: skill-server JWT + CORS. Need to verify before cutting
over.

## What ships in 3b itself

Just this document. No code change, no test change. The audit is the
deliverable.

Next code work: **Phase 3c** — delete the 25 pure proxies + FE URL swap.
