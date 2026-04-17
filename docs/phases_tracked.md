# Phases Tracked — Chat module production readiness

Living list of phase-sized work items for the Chat module, carried across
sessions. This supplements `CHAT_MODULE_REFACTOR_PLAN.md` (the architectural
plan) with a tactical, session-to-session snapshot of what's done, what's
next, and what's deliberately parked.

Last updated: 2026-04-17 (after Phase 1g — CI baseline).

---

## ✅ Shipped (on `credentialing-with-roster`)

Each row is a merged phase with a commit SHA you can `git show`. Tests for
each phase live in `tests/test_<phase_feature>.py`.

| Phase | What | Commit line |
|------:|:-----|:-----|
| 2.1 | `mobius-contracts` package: `AssistantEnvelope`, `ToolOutputEnvelope`, `CredentialingOptions` | `feat(contracts): scaffold mobius-contracts` |
| 0.6a | `ErrorEnvelope` contract (12-code union, user-safe/internal-detail split) | `feat(contracts): add ErrorEnvelope` |
| 2.5 | Token-aware Thompson sampling — Groq-tier TPM filter before bandit draw | `feat(router): token-aware Thompson sampling` |
| 0.6b | `ErrorEnvelope` wired into chat failure paths — no more raw provider JSON in UI | `feat(errors): wire ErrorEnvelope into chat failure paths` |
| 0.7 | ReAct smart-retry guard: don't repeat (tool, inputs) that failed; fail-fast on all-rounds-failed | `feat(react): smart-retry guard + fail-fast` |
| 0.8 | Source hygiene: drop sources from failed tool runs, dedup by `(doc, page)`, 30s scrape timeout | `feat(react): source hygiene + scrape timeout` |
| 2.3 | Thread-level sidebar with rule-based titles (schema migration 030) | `feat(threads): thread-level sidebar with real titles` |
| 0.10 | Stop PG pool thrash — `pg_pool` single-loop binding + `append_entry` no-op on sync contexts | `fix(db): stop pool-thrash that was hanging ReAct tool execution` |
| 0.11 | Neighbor-expansion page constraint + per-doc & total caps; citation index renumbering | `fix(rag): neighbor expansion page constraint + caps` |
| 0.12 | Softer integrator-fallback message, structured logging at every fallback | `feat(errors): soften fallback string` (part of 0.12+0.13 merge) |
| 0.13 | Auto-retry on recoverable `ErrorEnvelope` codes — honors `retry_after_seconds` (≤30s), one retry | `feat(errors): auto-retry recoverable errors` |
| 0.14 | BLENDED `direct_answer` includes inline specifics; UI surfaces `definitions` section by default | `fix(answer): richer BLENDED direct_answer + expanded default visibility` |
| 0.15 | Mode gradient: FACTUAL 1-line / BLENDED 1-3 sentences / CANONICAL paragraph; substantive bullets across all modes | `fix(answer): mode-gradient for direct_answer length + substantive sections` |
| 1a | First main-split slice — `/chat/history/*` extracted to `app/api/history.py` as proof-of-pattern | `refactor(api): extract /chat/history router` |
| 1b | Feedback + QC router — 6 endpoints extracted to `app/api/feedback.py`; each endpoint audited against its migration for Postgres persistence | `refactor(api): extract /chat feedback + QC router with PG persistence audit` |
| 1c | Credentialing-runs + NPI lookup router — 15 endpoints extracted to `app/api/credentialing.py`. **main.py: 3,125 → 2,401 lines (−23% across 1a+1b+1c).** Staging ground for Phase 3 (credentialing → own package) | `refactor(api): extract credentialing-runs + NPI lookup router` |
| 1d | Roster router — 26 endpoints (`/chat/roster-reconcile/*`, `/chat/roster-truth/*`, `/chat/roster-org/*`) extracted to `app/api/roster.py` via mechanical block-extraction. **main.py: 2,401 → 1,527 lines (−51% total across 1a-1d).** | `refactor(api): extract roster-reconcile + roster-truth + roster-org router` |
| 1e | Shared helper consolidation + CI-style hygiene guard. `_task_manager_base` was duplicated in main.py + 2 routers — now single-sourced in `app/api/_common.py`. New `test_api_hygiene_guard.py` scans main.py for stray `@app.*` decorators on chat paths AND for helper re-duplication; both fail the suite. **main.py: 1,527 → 1,524 lines.** | `refactor(api): consolidate shared helpers + hygiene guard` |
| 3a | Gate credentialing routers behind `CHAT_CREDENTIALING_ENABLED` flag (default true). Chat-without-credentialing deployment path unblocked. 5 subprocess-based tests verify both modes. | `feat(deploy): gate credentialing routers behind CHAT_CREDENTIALING_ENABLED` |
| 3b | Audit of the 41 credentialing/roster endpoints → classification: **25 pure proxy**, **4 mixed**, **12 chat-only**. Output: `docs/phase_3b_credentialing_audit.md` with per-endpoint detail and recommended 3c/3d/3e work. Discovery-only; no code change. | `docs(audit): phase 3b credentialing audit` |
| 3c | **Full deletion of chat's credentialing HTTP surface** — all 41 endpoints removed from chat per the user's "credentialing is a skill, not a chat interface" direction (user accepted the FE breakage — will reintegrate via direct skill calls or new `/credentialing/*` router later). Files deleted: `app/api/credentialing.py`, `app/api/roster.py`, `tests/test_api_credentialing_router.py`, `tests/test_api_roster_router.py`, `tests/test_credentialing_gated.py`. `CHAT_CREDENTIALING_ENABLED` flag no longer relevant (moot — credentialing isn't in chat). Hygiene guards strengthened to lock in the removal. | `refactor(api): remove chat's credentialing + roster HTTP surface` |
| 0.17 | **Feedback fail-closed** — closes the silent-data-loss pattern from the 1b audit. New `CHAT_ENV` env var (default `dev`) gates behavior: missing `CHAT_RAG_DATABASE_URL` or missing `adjudication_feedback` / `llm_performance_feedback` tables now *raise* `FeedbackPersistenceError` in staging/prod (500 to caller) instead of logging DEBUG and returning. Dev ergonomics preserved. Error messages include the chat DB migration number (024 / 025) so ops debugging is one step. | `feat(storage): feedback fail-closed on missing DB / table` |
| 0.16 | Two production bugs from the 2026-04-17 test logs. **0.16a:** web_scrape timeout actually fires at the cap (was 30s + 8s worker-drain due to `ThreadPoolExecutor` `with` block waiting on `__exit__`; now uses explicit `shutdown(wait=False)` on timeout). **0.16b:** LLM-based JSON repair tier deleted — `_parse_answer_card` already ran stdlib+json_repair library, the third LLM call was pure overhead AND was responsible for hitting Groq daily-TPD quota. Saves ~$0.01 + 2-5s per malformed turn. | `fix: web_scrape timeout fires at cap + delete overhead LLM-repair tier` |
| 0.18 | **Silent retrieval-killer fixed.** Live-test log showed `after normalize: len=5 → before context build: len=0` — RAG API returned 5 relevant chunks but the `confidence_min=0.5` filter in `non_patient_rag.py` only checked `match_score` / `confidence` field names (legacy inline-BM25 shape). RAG API chunks carry `rerank_score` + `confidence_label` — so every one scored as 0.0 and got silently dropped. Every ReAct turn was pivoting to google_search + web_scrape as if the corpus were empty. Fix: `_score_chunk_for_confidence_filter` helper falls through `match_score` → `confidence` → `rerank_score` → `confidence_label`-to-numeric map. | `fix(rag): confidence_min filter respects RAG API chunk shape` |
| 0.19 | **Tool-exhaustion block in ReAct retry guard.** 2026-04-17 live test exposed a gap in Phase 0.7: guard only blocks identical `(tool, inputs_sig)` pairs, so R1 `search_corpus` query="A" (5→0 kept) and R2 `search_corpus` query="B" (5→0 kept) both ran because the reasoner reformulated the query between rounds. Fix: per-tool consecutive-failure counter; after 2 failures with no intervening success, tool is blocked regardless of inputs_sig. `failure_hint_for_prompt` surfaces "Exhausted tools (pick a DIFFERENT tool, not a re-phrased query)" to the planner. | `feat(react): tool-exhaustion block in retry guard` |
| 1f.1 | **Tasks router extracted.** 8 `/chat/tasks/*` endpoints moved to `app/api/tasks.py`; `_task_proxy` consolidated into `app.api._common.task_proxy` (also used by `/chat/runs` aggregator). Hygiene guard gained a ratcheting `MAX_MAIN_PY_LOC` / `MAX_MAIN_PY_ENDPOINTS` ceiling so regression is impossible. **main.py: 1,528 → 1,408 LOC, 36 → 28 endpoints.** 15 new router tests + 10 hygiene tests green. | `refactor(api): extract /chat/tasks router (Phase 1f.1)` |
| 1g | **CI baseline.** New `.github/workflows/ci.yml` runs phase-regression tests on every push + PR to the integration branches. Two jobs: (a) pytest against a vendored minimal dep set — skips `pip install -r requirements.txt` because it contains the local-path `-e ../mobius-retriever`; (b) ruff lint, non-blocking first pass. Covers hygiene guard, tasks router, retry guard, 0.19 exhaustion block, 0.18 confidence filter, 0.16 scrape timeout. `test_ci_baseline.py` locks the workflow file in place (asserts file exists, Python minor pinned, concurrency-cancel on, every `CRITICAL_TEST_FILES` path wired into the run step). Verified by simulating CI in a clean venv: 90/90 green. Closes "no CI at all" gap surfaced in the comprehensive audit. | `feat(ci): add GitHub Actions baseline for phase-regression tests` |

**Phase 1g CI subset: 90/90 green** (hygiene + tasks router + retry guard + exhaustion + confidence filter + scrape timeout + ci-baseline guard). Full-suite total after 0.18 was 234; net-new since = 9 (0.19) + 15 (1f.1 router) + 2 (ratchet) + 8 (ci-baseline) = 268 reachable locally. CI runs only the self-contained subset until a follow-up phase teaches it to check out sibling packages.

**main.py shrinkage across Phase 1: 3,125 → ~1,528 lines (−51%).**
**Chat router surface after 3c: 2 routers (history, feedback) mounting 10 endpoints. The 51 routes mounted after 1d → 10 after 3c.**

### Feedback persistence audit (done during 1b)

All six feedback endpoints verified to write to Postgres:

| Endpoint | Storage fn | Table | Migration |
|---|---|---|---|
| `POST /chat/feedback/{cid}` | `insert_feedback` | `chat_feedback` | 003 |
| `POST /chat/source-feedback/{cid}` | `insert_source_feedback` | `chat_source_feedback` | 006 |
| `POST /chat/adjudication-feedback/{cid}` | `insert_adjudication_feedback` | `adjudication_feedback` | 025 |
| `POST /chat/llm-performance-feedback/{cid}` | `insert_llm_performance_feedback` | `llm_performance_feedback` | 024 |
| `POST /chat/qc-audit/{cid}` | `update_turn_qc_audit` | `chat_turns.qc_audit` JSONB | 023 |
| `POST /chat/qc-user-score/{cid}` | `update_turn_qc_audit` | `chat_turns.qc_audit` JSONB | 023 |

Softness observed (tracked as Phase 0.17 below):
- All `insert_*` silently return if `CHAT_RAG_DATABASE_URL` is unset.
- `adjudication_feedback` + `llm_performance_feedback` swallow "relation does not
  exist" as DEBUG — silent data loss if migrations 024/025 never ran.

Observed end-to-end impact (per the 2026-04-17 test session):

- Citation count per turn dropped from **1,078 → ~5**.
- ReAct tool hangs gone (pool thrash eliminated).
- No Groq JSON reaches the chat UI; rate-limit-class errors render as
  "The model is temporarily busy…" then auto-retry once.
- Bandit correctly avoids Groq models for large prompts; rotates cleanly
  across Gemini Flash / Haiku / Gemini Pro.

---

## 🎯 Next up — structural phases

Order is intentional: each unlocks the next.

### Phase 1 — Split `main.py` into routers
Current `app/main.py` is 3,125 LOC with 86 FastAPI endpoints. Splitting it
into `app/api/{chat,tasks,credentialing,strategy,rag,admin,pages,health}.py`
is the wedge that makes subsequent refactors safe — the main.py monolith is
currently the single blocker for both (a) enforcing typed `response_model=`
contracts on every endpoint and (b) extracting credentialing cleanly.

Estimated: 3–5 days. No user-visible change.

### Phase 3 — Extract credentialing into its own package
Credentialing currently lives in `mobius-chat/app/services/credentialing_*`
and represents ~30% of the chat codebase. Goal: a `credentialing/` package
that Chat talks to via a typed client (`CredentialingClient`), starting
in-process and flipping to HTTP via a `CREDENTIALING_MODE` switch.

Unlocks: Chat-without-credentialing can go to prod as its own deployable,
matching the user's module sequencing ("Chat first, Credentialing later").

Estimated: 3 days after Phase 1.

---

## 📋 Tracked — smaller items

### (0.16 shipped — see the Shipped table above)

### (reserved for future hardening items)
- Scrape observed to run ~38s on one test turn despite the 30s guard
  (Phase 0.8); the `ThreadPoolExecutor` wait races setup time. Move
  the timeout to wrap from "Using web_scrape…" emit onward.
- JSON-repair path still hits Groq `llama-3.3-70b-versatile` and
  exhausted its **daily** TPD quota during the test session. Either
  route repair calls through a non-Groq model unconditionally, or
  declare Groq daily TPD limits on the `ModelSpec` so Phase 2.5's
  filter can keep them out of repair too.
- Scope: ~1 hour.

### Emit → task-manager event migration (user flagged 2026-04-17)
The ReAct loop currently emits progress as transient UI strings
(`emit("◌ Searching the web…")`, `emit("  ⊘ web_scrape timed out")`,
`emit("  ⊘ search_corpus exhausted — pivoting…")`). These are visible
during the turn but *lost after the stream closes* — there's no
persistent trail of what tools fired, what retried, what got blocked
by the exhaustion guard, or what evidence was ultimately used.

The fix is to post structured events to `task-manager` (the skill we
just wired `_task_proxy` into) alongside the emits, so every turn
leaves a queryable timeline. Minimum event shape:

    {"turn_id", "round", "kind": "tool_start|tool_ok|tool_skip|tool_fail",
     "tool", "inputs_sig", "error_code", "skip_reason", "elapsed_ms",
     "chunks_kept", "chunks_dropped"}

Scope: one tiny `react_events` helper that both emits (unchanged UI
behavior) AND posts to task-manager. Wire it at the 4-5 places in
`react_loop.py` that already call `emit()` with a status glyph. Keep
it best-effort (don't let a task-manager failure crash the turn).

Unlocks: observability for the retry guard + exhaustion block
(Phase 0.19) — without this we can't answer "how often does
tool_exhausted actually fire in prod" from data.

Scope: ~2 hours.

### 1.2 — Typed DB contract + shared pool
Follow-up to Phase 0.10 (pool thrash fix). Move all DB access through a
typed client interface per domain (chat, credentialing, rag, strategy,
task-manager) with one shared pool per process. Mirrors the mobius-contracts
pattern. Best done with Phase 3 so the credentialing DB seam is designed
from the start.

Scope: ~2 days.

### 2.3b — Thread restoration UI + LLM titles + backfill
Sidebar currently shows thread titles (Phase 2.3) but clicking one pre-fills
the input with the title rather than re-opening the thread. Follow-ups:
- Click-to-restore: load turns from `chat_turn_messages`, render them in
  the main pane.
- LLM-generated titles for threads where the rule-based title is thin
  (e.g. `"Lookup: H0036"` → `"H0036 coverage under Sunshine Health"`).
- One-time backfill script for pre-migration-030 threads.

Scope: ~1 day.

---

## 🌱 Tracked — product-level capabilities

### Skill UI integration pattern (architecture decision, not yet executed)

**Decision date:** 2026-04-17 after Phase 3c.

**Chosen approach: Option D — envelope-first with progressive depth.**

Skills publish typed envelopes only (data contracts), not UI components.
Chat FE renders them inline using its own components for the common case.
For deep workflows, chat links out to the skill's standalone UI (which
reads the same envelopes). This is the pattern for every future skill
integration, not just credentialing.

Shape:

```
mobius-contracts/envelopes/
    assistant.py        # already exists — chat's outer envelope
    credentialing.py    # typed outputs of credentialing skill
    task_manager.py     # typed outputs of task-manager skill
    …                   # one per skill
```

- ReAct tool returns a typed skill envelope.
- Chat wraps it in a new `AssistantEnvelope` UIBlock variant
  (e.g. `CredentialingReportBlock(envelope=...)`).
- Chat FE renders the block inline (summary card + metrics + deep link).
- "Open full report →" navigates to the skill's standalone UI at
  `envelope.deep_link_url`.

**Principles:**
- Envelopes are the ONLY cross-service contract.
- Skills never push DOM/CSS/JS into chat (no microfrontend complexity).
- Chat FE owns inline rendering — one unified look across the thread.
- Skills own standalone UIs — ops/admin/deep-workflow control panels.
- `mobius-contracts` is the schema registry; every envelope has a
  Pydantic model with `schema_name` + `version`.

**What this blocks for now:**
- The credentialing skill still returns prose from its ReAct tools.
  Typing its outputs as envelopes is a 0.5-day move when we're ready.
- The 65+ chat-FE callers that broke in Phase 3c will eventually be
  re-integrated either by (a) calling the skill URL directly, or (b)
  reading typed envelopes from ReAct tool responses and rendering new
  UIBlock variants. Decision per-caller when we get to it.

**When to execute:** when the first skill integration actually needs it.
Likely triggers: re-enabling the credentialing UI flows that broke in
3c, or standing up a new skill that needs chat display.

### Real-time RAG (Instant RAG skill)
Per `project_instant_rag_skill.md` in user memory.

**Use case:** user uploads a PDF/doc and queries against it in the current
chat without waiting for batch ingest.

**Expected architecture:**
- Standalone skill producing typed envelopes for agent consumers.
- Ephemeral 7-day TTL on uploaded docs.
- Reuses chat upload path + task-manager + Path B retrieval.

**How it touches Chat:**
- New ReAct tool: `search_uploaded_document(upload_id, query)`.
- Tool registry entry in `tool_manifest.py` + handler in
  `react_loop._execute_tool`.
- UI: upload widget → emit `thread_document_uploaded` → upload_id
  available to the next turn's reasoning context.
- Envelope: define a new `InstantRagEnvelope` in
  `mobius-contracts/envelopes/instant_rag.py` that follows the same
  typed-contract patterns as `ToolOutputEnvelope`.

**Why it matters for prod-readiness:**
- The H0036 test session showed the integrator refusing on thin corpus.
  Real-time RAG is the highest-leverage fix — instead of waiting for
  docs to be ingested, the user brings them on demand.
- Turns a retrieval-quality problem into a user-agency feature.

**Corpus coverage** (separate from real-time RAG) is a RAG-team concern,
out of scope for the Chat module's production-readiness refactor.

---

## 🔮 Later

| Phase | What |
|------:|:-----|
| 4 | Frontend thinning — pure-presentation JS, TS types generated from OpenAPI |
| 5 | Prod hardening — prod deploy script, CI/CD, uptime/5xx monitoring, Cloud Armor rate-limit |

---

## How to use this doc

- Before picking up work, skim "Next up" and "Tracked" to see what's
  queued. Don't re-plan from scratch.
- When a phase ships, move its row from "Tracked" to "Shipped" and add
  the commit line.
- When a new capability surfaces, add it to "Tracked — product-level"
  or "Tracked — smaller items" with enough detail that a fresh session
  can pick it up without re-investigating.
- This doc is read by humans AND by future agent sessions that lose
  chat context. Keep it self-contained — describe scope in enough
  detail to start work from, not just name it.
