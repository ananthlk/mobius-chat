# Phases Tracked — Chat module production readiness

Living list of phase-sized work items for the Chat module, carried across
sessions. This supplements `CHAT_MODULE_REFACTOR_PLAN.md` (the architectural
plan) with a tactical, session-to-session snapshot of what's done, what's
next, and what's deliberately parked.

Last updated: 2026-04-17 (after Phase 0.15 merge + test session).

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

**Total unit tests across these phases: 209/209 green.**
**main.py shrinkage across Phase 1: 3,125 → 1,524 lines (−51%).**

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

### 0.17 — Feedback persistence hardening
Two softness patterns discovered during Phase 1b's persistence audit:

1. **Silent `CHAT_RAG_DATABASE_URL` unset.** All five `insert_*` fns in
   `app/storage/feedback.py` log a WARNING and silently return when the env
   var isn't set. Correct for dev-without-DB but dangerous in prod — a
   misconfigured env = silent feedback loss. Fix: fail-closed in non-dev.
2. **Silent "relation does not exist".** `insert_adjudication_feedback` and
   `insert_llm_performance_feedback` swallow the error as DEBUG, so if
   migrations 024/025 never ran the feedback silently vanishes. Fix: raise
   a typed error the first time, or at minimum log at WARNING.

Scope: ~30 min.

### 0.16 — Tighten web_scrape timeout + JSON-repair provider swap
- Scrape observed to run ~38s on one test turn despite the 30s guard
  (Phase 0.8); the `ThreadPoolExecutor` wait races setup time. Move
  the timeout to wrap from "Using web_scrape…" emit onward.
- JSON-repair path still hits Groq `llama-3.3-70b-versatile` and
  exhausted its **daily** TPD quota during the test session. Either
  route repair calls through a non-Groq model unconditionally, or
  declare Groq daily TPD limits on the `ModelSpec` so Phase 2.5's
  filter can keep them out of repair too.
- Scope: ~1 hour.

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
