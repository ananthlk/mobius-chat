# SPEC: Fix `llm_calls.correlation_id` / `thread_id` propagation

**Status:** Draft / proposal
**Audience:** Engineers working on `mobius-chat` and any service that calls back to `chat /internal/skill-llm` (`mobius-skills/vibe`, `mobius-skills/instant-rag`, `mobius-rag`, `mobius-skills-core`, `mobius-skills/healthcare`, `mobius-skills/web-scraper`, `mobius-skills/google-search`, `mobius-doc-reader`).

---

## 1. TL;DR

Across the last 48 hours of production traffic, **76% of `llm_calls` rows have NULL `correlation_id`** and **100% have NULL `thread_id`**. As a result, the per-turn admin dashboard at `/chat/admin/queries` shows misleadingly low LLM-call counts per turn (often 1, when a typical RAG turn fires 5–10).

This is a propagation bug across a three-leg round-trip:

```
chat process → skill service → chat /internal/skill-llm → llm_calls (NULL cid here)
```

There is exactly **one writer** in the system (`mobius-chat/app/services/llm_manager.py:222`), and the path that does propagate `correlation_id` works perfectly: when `cid` is set, it matches a `chat_turns` row 100% of the time. The fix is wiring, not architecture.

---

## 2. Diagnostic data

Run against the live `mobius_chat` database via Cloud SQL Auth Proxy on `2026-05-06`:

### 2-day window (101 rows)

| metric | count | % |
|---|---|---|
| total `llm_calls` | 101 | 100% |
| `correlation_id` set | 24 | 24% |
| `correlation_id` NULL | 77 | **76%** |
| `thread_id` set | 0 | **0%** |
| `thread_id` NULL | 101 | **100%** |
| recoverable via thread (cid NULL & tid set) | 0 | 0% |
| both NULL | 77 | 76% |
| cids that match a `chat_turns` row | 24/24 | **100%** |

### Top orphan stages (NULL `correlation_id`, last 2 days)

| stage | model | provider | rows |
|---|---|---|---|
| `vibe` | gemini-2.5-flash | vertex | 21 |
| `rag_strategy_b_synth` | gemini-2.5-flash | vertex | 14 |
| `lexicon_triage` | gemini-2.5-flash | vertex | 12 |
| `rag_strategy_a_synth` | gemini-2.5-flash | vertex | 10 |
| `rag_strategy_c_validate` | gemini-2.5-flash | vertex | 8 |
| `vibe` | claude-haiku-4-5 | anthropic | 7 |
| `rag_strategy_d_external` | gemini-2.5-flash | vertex | 3 |
| `org_intel_synthesis` | gemini-2.5-flash | vertex | 2 |

Every orphan stage runs in a **satellite service** (vibe, instant-rag, etc.), then makes its actual LLM call by POSTing to chat's `/internal/skill-llm` endpoint. None of the orphans originate from chat's own react/integrator/adjudicator pipeline.

---

## 3. Architecture (the round-trip)

```
┌──────────────────┐                   ┌──────────────────────┐
│   mobius-chat    │   1. dispatch     │   satellite service  │
│   (chat_turns,   │ ───────────────►  │   (vibe, instant-rag,│
│    react, …)     │   POST /vibe etc. │    healthcare, …)    │
│                  │                   │                      │
│                  │ ◄──────────────── │                      │
│                  │  4. response      │   2. needs an LLM    │
│                  │                   │      ↓                │
│ ┌──────────────┐ │  3. POST          │                      │
│ │ /internal/   │◄┼─────────────────  │ llm_complete(...)    │
│ │ skill-llm    │ │  /internal/       │                      │
│ └──────┬───────┘ │   skill-llm       │                      │
│        │         │                   │                      │
│  llm_manager     │                   └──────────────────────┘
│  .generate()     │
│        │
│        ▼
│  llm_analytics
│  .build_record()
│  ._write_async()
│        │
│        ▼
│   ┌─────────────┐
│   │ llm_calls   │  ← cid/tid land here only if every leg
│   │  table      │    above passed them through
│   └─────────────┘
└──────────────────┘
```

The cid/tid orphan happens because:

- **Leg 1 (chat → satellite):** chat does NOT include `correlation_id` / `thread_id` in the JSON body sent to the satellite for many skills.
- **Leg 3 (satellite → `/internal/skill-llm`):** satellite's LLM client (e.g. `mobius-skills/vibe/app/llm_client.py`) accepts `correlation_id` but defaults to None, and only forwards it to `/internal/skill-llm` when set.
- **Endpoint accepts NULL silently:** `/internal/skill-llm` declares `correlation_id: str | None = None`. NULL bodies pass validation and produce orphan rows.

---

## 4. Root cause summary

1. **Single writer is correct.** `mobius-chat/app/services/llm_manager.py:222–243` writes `correlation_id` faithfully when the caller passes one.
2. **Plumbing is incomplete.** Across the round-trip, three legs each have a "soft fail" — accept None, pass None, write None. Any one missing link orphans the row.
3. **No invariant enforced.** Schema allows NULL on both columns. Endpoint allows NULL on both. Skill-side LLM clients allow NULL on both. The system has no place where a missing cid/tid causes a loud failure, so a regression (like the recent `thread_id` going 100% NULL) ships silently.

---

## 5. Fix plan

Three coordinated changes, one per leg of the round-trip. Plus a schema invariant to prevent regression.

### Fix A. `mobius-chat` — outbound skill calls must include `correlation_id` + `thread_id`

**Files:** every `mobius-chat/app/skills/builtin/*.py` and any service file that builds an HTTP body for a satellite.

**Concrete sites to audit:**
- `app/skills/builtin/vibe.py` — POSTs to `CHAT_SKILLS_VIBE_URL`
- `app/skills/builtin/healthcare_query.py` — POSTs to `CHAT_SKILLS_HEALTHCARE_URL`
- `app/skills/builtin/web_scrape.py` — POSTs to `WEB_SCRAPER_URL`
- `app/skills/builtin/google_search.py` — POSTs to `GOOGLE_SEARCH_URL`
- `app/skills/builtin/transform_previous_answer.py`
- `app/skills/builtin/cached_answer_lookup.py`
- `app/skills/builtin/fetch_document.py`
- `app/skills/builtin/document_upload_skill.py`
- `app/skills/builtin/list_thread_document_uploads.py`
- `app/api/doc_reader.py` (forwards to mobius-doc-reader)
- Anywhere `app/services/instant_rag*.py` makes an HTTP call

**Required change for each:**

```python
# BEFORE
req_body = {
    "system": system,
    "user": user,
    "stage": stage,
}

# AFTER
req_body = {
    "system": system,
    "user": user,
    "stage": stage,
    "correlation_id": call.correlation_id,   # source from SkillCall ctx
    "thread_id":      call.thread_id,
}
```

`SkillCall` (the type passed into every `_run_*` handler) must carry both ids. If it does not, fix the dispatcher (`app/skills/registry.py` / `app/pipeline/orchestrator.py`) to populate them at construction time.

### Fix B. Satellite services — propagate `correlation_id` / `thread_id` end-to-end

**Repos:** `mobius-skills/vibe`, `mobius-skills/instant-rag`, `mobius-skills-core`, `mobius-skills/healthcare`, `mobius-skills/web-scraper`, `mobius-skills/google-search`, `mobius-rag`, `mobius-doc-reader`.

**Pattern (vibe shown; mirror in every satellite):**

```python
# Existing in mobius-skills/vibe/app/llm_client.py
def llm_complete(
    system: str,
    user: str,
    stage: str = "badge",
    max_tokens: int = 60,
    correlation_id: str | None = None,   # ← already present
    thread_id: str | None = None,        # ← ADD
    timeout_sec: float = 30.0,
) -> tuple[str, dict[str, Any]]:
    ...
    payload: dict[str, Any] = {
        "system": system or "",
        "user": user or "",
        "stage": stage,
        "max_tokens": max_tokens,
    }
    # BEFORE: conditional sending hides bugs
    # if correlation_id:
    #     payload["correlation_id"] = correlation_id

    # AFTER: always send both, fail fast if missing
    if not correlation_id or not thread_id:
        raise RuntimeError(
            f"vibe.llm_complete called without correlation_id/thread_id "
            f"(stage={stage}). All callers must propagate."
        )
    payload["correlation_id"] = correlation_id
    payload["thread_id"]      = thread_id
```

**Then, every caller of `llm_complete()` inside that satellite must pass cid/tid** — typically from the inbound HTTP request body (which now carries them per Fix A).

**Concrete sites in vibe (representative):**
- `mobius-skills/vibe/app/main.py` — `POST /vibe` handler. Pull `correlation_id` and `thread_id` from request body. Thread them through every internal function that eventually calls `llm_complete()`.

**Same pattern repeats** in instant-rag, mobius-skills-core, healthcare, etc. Each has its own LLM client that does the `/internal/skill-llm` POST.

### Fix C. `mobius-chat` — make `/internal/skill-llm` REQUIRE `correlation_id` and `thread_id`

**File:** `mobius-chat/app/main.py:1551–1561`

```python
# BEFORE
class SkillLLMRequest(BaseModel):
    system: str = ""
    user: str = ""
    stage: str = "credentialing_draft"
    max_tokens: int = 4096
    correlation_id: str | None = None   # ← optional
    thread_id: str | None = None        # ← optional
    mode: str | None = None

# AFTER
class SkillLLMRequest(BaseModel):
    system: str = ""
    user: str = ""
    stage: str = "credentialing_draft"
    max_tokens: int = 4096
    correlation_id: str   # required — Pydantic returns 422 if missing
    thread_id: str        # required
    mode: str | None = None
```

The endpoint already passes both to `llm_manager.generate(...)` correctly (line 1597–1598), so no other change needed here. Pydantic will reject missing-field requests with 422 — turns the silent-NULL bug into a loud failure.

### Fix D. Schema invariant on `llm_calls`

**File:** `mobius-chat/db/schema/035_llm_calls_not_null_correlation.sql` (new migration)

```sql
-- 035: enforce correlation_id and thread_id are present on every llm_calls row.
-- Prerequisite: A/B/C above must be deployed and verified for >=24h with
-- zero orphans before running this. Otherwise the INSERT will fail in prod.

ALTER TABLE llm_calls
    ALTER COLUMN correlation_id SET NOT NULL,
    ALTER COLUMN thread_id      SET NOT NULL;
```

This is the invariant that prevents future regression. Once in place, any caller that forgets to propagate cid/tid breaks the build, not the dashboard.

---

## 6. Rollout sequence

Strict order — do not skip:

1. **Land Fix A** (chat outbound). Deploy. Verify cid is present in skill request bodies (log a sample).
2. **Land Fix B in every satellite**, one repo at a time. Each satellite must propagate cid through every internal call site to its `llm_complete()`. Deploy each. Verify orphan rate drops with each rollout (see verification queries below).
3. **Land Fix C** (chat endpoint requires fields). Deploy. Any old satellite still in flight will start returning 422 — that's intended.
4. **Wait 24 hours** with all four services on the new code. Confirm orphan rate is zero.
5. **Land Fix D** (NOT NULL constraint). This is the point-of-no-return — once it's live, any orphan attempt is a hard error.

Do **not** ship Fix C or D before Fix A and B are live in every satellite, or you'll start dropping legitimate LLM calls.

---

## 7. Acceptance criteria

After Fix A + B + C are live in every satellite:

```sql
-- Run via cloud-sql-proxy on the dev or prod chat database.
SELECT
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE correlation_id IS NULL) AS cid_null,
    COUNT(*) FILTER (WHERE thread_id IS NULL)      AS tid_null,
    ROUND(
        COUNT(*) FILTER (WHERE correlation_id IS NULL) * 100.0 / COUNT(*),
        2
    ) AS pct_orphaned
FROM llm_calls
WHERE ts > NOW() - INTERVAL '24 hours';
```

Expected: `pct_orphaned ≈ 0.00`.

Per-stage check — no stage should have orphans:

```sql
SELECT
    stage,
    COUNT(*) FILTER (WHERE correlation_id IS NULL) AS orphans,
    COUNT(*) AS total
FROM llm_calls
WHERE ts > NOW() - INTERVAL '24 hours'
GROUP BY 1
HAVING COUNT(*) FILTER (WHERE correlation_id IS NULL) > 0
ORDER BY orphans DESC;
```

Expected: zero rows returned.

Per-turn dashboard sanity — typical RAG turn should now show 5–10 LLM calls:

```bash
curl -sS 'https://mobius-chat-ortabkknqa-uc.a.run.app/chat/admin/queries?limit=50' \
    | jq '[.rows[].llm_call_count] | {min: min, max: max, avg: (add / length)}'
```

Expected: `avg` rises from ~0.04 (current) to ≥3.0; `max` should reach ~10.

---

## 8. Out of scope

These are real concerns but tracked separately:

- **`doc-reader` and `instant-rag` LLM-call telemetry.** If those services do **not** route their LLM calls through chat's `/internal/skill-llm` (i.e., they make direct Anthropic / Vertex calls), their telemetry won't reach `llm_calls` regardless of this fix. Audit each satellite: if it has its own provider client, either migrate it to use `/internal/skill-llm`, or accept the gap and document it.
- **A `route` / `handled_by` tag column on `chat_turns`** so the dashboard can label "this turn was handled by doc-reader, expected zero in-process calls." Useful but orthogonal.
- **BuildKit migration in chat's Cloud Build pipeline** — current `--cache-from` setup occasionally serves stale frontend bundles. Use `--no-cache` for any frontend-touching deploy until BuildKit is on.

---

## 9. Open questions for the team

1. **Why did `thread_id` go from 54% populated (30-day window) to 0% populated (2-day window)?** A recent commit broke propagation. `git log --since="3 days ago"` on chat's `main.py`, `services/llm_manager.py`, and `services/llm_analytics.py` should surface it. Bisecting before broad fixes will clarify whether Fix A/B/C are sufficient or there's a chat-internal regression to also revert.

2. **Do `doc-reader` and `instant-rag` use `/internal/skill-llm`** or do they have direct provider integrations? If direct, they need the same propagation fix internally PLUS we need to decide whether to consolidate them onto `/internal/skill-llm`.

3. **Is `correlation_id` available at the time satellites need it?** All satellites are called from chat's orchestrator, which knows the cid. So yes — but verify `SkillCall` carries it everywhere before relying on it.

---

## 10. Reference

- Single LLM-calls writer: `mobius-chat/app/services/llm_manager.py:222–243` → `app/services/llm_analytics.py:_write_async`
- Endpoint: `mobius-chat/app/main.py:1551–1610` (`/internal/skill-llm`)
- Per-turn dashboard reading the table: `mobius-chat/app/storage/queries_dump.py`
- Aggregate report (rolling, no per-turn join): `mobius-chat/app/storage/llm_router_report.py`
- Satellite LLM-client example: `mobius-skills/vibe/app/llm_client.py`
- Schema: `mobius-chat/db/schema/020_llm_analytics.sql`

---

## 11. Diagnostic queries (run via Cloud SQL Auth Proxy)

```bash
# Connect (assumes cloud-sql-proxy already running on :5433):
export PGPASSWORD="$(gcloud secrets versions access latest --secret=db-password \
    --project=mobius-os-dev)"
psql -h localhost -p 5433 -U postgres -d mobius_chat
```

```sql
-- Orphan rate (last 2 days)
SELECT
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE correlation_id IS NOT NULL) AS cid_set,
    COUNT(*) FILTER (WHERE correlation_id IS NULL)     AS cid_null,
    COUNT(*) FILTER (WHERE thread_id     IS NOT NULL)  AS tid_set,
    COUNT(*) FILTER (WHERE thread_id     IS NULL)      AS tid_null
FROM llm_calls
WHERE ts > NOW() - INTERVAL '2 days';

-- Top orphan stages
SELECT stage, model, provider, COUNT(*) AS n
FROM llm_calls
WHERE ts > NOW() - INTERVAL '2 days' AND correlation_id IS NULL
GROUP BY 1, 2, 3
ORDER BY n DESC
LIMIT 20;

-- Match rate to chat_turns (when cid is set)
WITH calls AS (
    SELECT correlation_id FROM llm_calls
    WHERE ts > NOW() - INTERVAL '2 days' AND correlation_id IS NOT NULL
)
SELECT
    COUNT(*) AS calls_with_cid,
    COUNT(DISTINCT correlation_id) AS distinct_cids,
    COUNT(*) FILTER (WHERE EXISTS (
        SELECT 1 FROM chat_turns t WHERE t.correlation_id = calls.correlation_id
    )) AS cids_matching_turn
FROM calls;
```
