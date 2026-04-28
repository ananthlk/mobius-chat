# Chat-side integration — how mobius-chat will consume this service

Once the new repo is up and a Cloud Run URL exists, the chat agent
adds three things and removes two. Total ~1 day of work; the
patterns are already in place from the `corpus_search` skill that
shipped this week.

## What chat adds

### 1. New env: `MOBIUS_CACHE_URL`

In `mobius-chat/deploy/dev.env`:

```
# mobius-answer-cache service URL. Skill consumers
# (cache_lookup, cache_write) post to {MOBIUS_CACHE_URL}/api/skills/v1/...
# with X-Caller=mobius_chat and X-Caller-Id=<chat correlation_id> headers.
# When unset, the cache_lookup skill returns no_sources cleanly and
# cache_write is a no-op — chat keeps working without the cache.
MOBIUS_CACHE_URL=https://mobius-answer-cache-ortabkknqa-uc.a.run.app
```

In `mobius-chat/scripts/deploy.sh` `SET_ENV_VARS`:

```bash
"MOBIUS_CACHE_URL=${MOBIUS_CACHE_URL:-}"
```

### 2. New skill: `cache_lookup` consumer

File: `mobius-chat/app/skills/builtin/cache_lookup.py`

Mirror of `mobius-chat/app/skills/builtin/corpus_search.py`. The
consumer skill:

* Reads `MOBIUS_CACHE_URL`. Returns `no_sources` cleanly if unset.
* Builds the lookup body from `SkillCall` inputs + `active_context`.
* POSTs to `{MOBIUS_CACHE_URL}/api/skills/v1/cache_lookup` with
  `Content-Type`, `X-Caller: mobius_chat`,
  `X-Caller-Id: <pipeline_ctx.correlation_id>`.
* Maps the response to a `SkillEnvelope`:
  * `text`: rendered candidate block (number them `[1]…[N]`,
    show similarity + age + question + answer-head per candidate)
  * `sources`: one `SourceRef` per candidate, with the cached
    answer in the `text` field
  * `signal`: `cache_hit` if any candidates returned, else
    `no_sources`
  * `extra["candidates"]` + `extra["telemetry"]` (full envelope so
    the orchestrator's cache_mode logic can inspect)
* Emits a `cache_lookup_fired` envelope into thinking_log (same
  pattern as `corpus_search`'s `retrieval_trace`).

Skeleton (~80 lines):

```python
from __future__ import annotations
import json, logging, os, time, urllib.error, urllib.request, uuid
from typing import Any
from app.skills.registry import SkillCall, SkillEnvelope, SkillSpec, SourceRef, register

logger = logging.getLogger(__name__)
_TIMEOUT_S = 8.0  # cache lookup must NEVER block a turn
_PATH = "/api/skills/v1/cache_lookup"

def _run(call: SkillCall) -> SkillEnvelope:
    base = (os.environ.get("MOBIUS_CACHE_URL") or "").strip()
    if not base:
        return SkillEnvelope(text="", signal="no_sources", extra={"error": "cache_url_unset"})
    inputs = call.inputs or {}
    question = (inputs.get("question") or call.question or "").strip()
    active = call.active_context or {}
    body = {
        "question": question,
        "config_sha": inputs.get("config_sha"),
        "filters": {
            "payer": active.get("payer"),
            "state": active.get("state") or active.get("jurisdiction"),
            "program": active.get("program"),
            "max_age_days": int(inputs.get("max_age_days", os.environ.get("CACHE_ASSIST_DEFAULT_MAX_AGE_DAYS") or 14)),
            "domain_tags": inputs.get("domain_tags"),
        },
        "min_similarity": float(inputs.get("similarity_floor", 0.85)),
        "k": int(inputs.get("top_k", 5)),
        "caller": "mobius_chat",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Caller": "mobius_chat",
    }
    cid = getattr(call.pipeline_ctx, "correlation_id", None) or str(uuid.uuid4())
    headers["X-Caller-Id"] = cid

    req = urllib.request.Request(
        base.rstrip("/") + _PATH,
        data=json.dumps(body).encode(),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        logger.warning("cache_lookup transport failed: %s", e)
        return SkillEnvelope(text="", signal="no_sources",
                             extra={"error": f"{type(e).__name__}: {e}"})
    candidates = data.get("candidates") or []
    if not candidates:
        return SkillEnvelope(text="", signal="no_sources",
                             extra={"telemetry": data.get("telemetry") or {}})
    # Render top-K block + sources
    sources = [
        SourceRef(
            document_name=f"cached_answer[{i+1}]",
            index=i+1,
            text=(c.get("answer") or "")[:1500],
            source_type="cached_answer",
            extra={"similarity": c.get("similarity"), "age_days": c.get("age_days"),
                   "candidate_id": c.get("candidate_id")},
        )
        for i, c in enumerate(candidates)
    ]
    text_lines = [
        "CACHED PRIOR ANSWERS (semantically similar past turns):",
    ]
    for i, c in enumerate(candidates, 1):
        text_lines.append(
            f"[{i}] sim={c.get('similarity'):.3f} · age={c.get('age_days')}d\n"
            f"    Q: {(c.get('question') or '')[:200]}\n"
            f"    A: {(c.get('answer') or '')[:500]}"
        )
    return SkillEnvelope(
        text="\n\n".join(text_lines),
        sources=sources,
        signal="cache_hit",
        extra={"candidates": candidates, "telemetry": data.get("telemetry") or {}},
    )

SPEC = SkillSpec(
    name="cached_answer_lookup",
    description="...same description as the legacy cached_answer.py...",
    handler=_run,
    inputs_schema={"type": "object", "properties": {
        "question": {"type": "string"},
        "similarity_floor": {"type": "number"},
        "max_age_days": {"type": "integer"},
        "top_k": {"type": "integer"},
        "domain_tags": {"type": "array", "items": {"type": "string"}},
        "config_sha": {"type": "string"},
    }},
    requires_jurisdiction=False,
    follow_up_capable=False,
    supports_modes=("copilot", "quick"),
    source="builtin",
    visible_to_planner=True,
)
register(SPEC)
```

### 3. Cache write — fire-and-forget after `_publish_completed`

In `mobius-chat/app/pipeline/orchestrator.py`, replace the existing
`schedule_cache_write` call site (which today imports
`app.services.cache_writer.schedule_cache_write`) with an HTTP
POST to `{MOBIUS_CACHE_URL}/api/skills/v1/cache_write` on a daemon
thread. Same fire-and-forget pattern; new payload shape:

```python
def schedule_cache_write_v2(ctx, payload):
    if not (os.environ.get("MOBIUS_CACHE_URL") or "").strip():
        return
    # Reuse the should_cache gate from the legacy cache_writer.py —
    # that logic stays in chat (decisions need ctx.retrieval_signals,
    # ctx.cache_influence, etc. which the cache service doesn't have).
    if not _should_cache(ctx, payload)[0]:
        return
    body = {
        "correlation_id":  ctx.correlation_id,
        "thread_id":       ctx.thread_id,
        "question":        ctx.message,
        "answer":          payload.get("message") or "",
        "skill_envelope":  payload.get("assistant_envelope") or {},
        "config_sha":      payload.get("config_sha"),
        "filters": {
            "payer":           (ctx.active_context or {}).get("payer"),
            "state":           (ctx.active_context or {}).get("state"),
            "program":         (ctx.active_context or {}).get("program"),
            "authority_level": (ctx.active_context or {}).get("authority_level"),
        },
        "domain_tags":     ctx.domain_tags or [],
        "qc_passed":       bool(getattr(ctx, "qc_audit_passed", True)),
        "thumbs_down":     False,
        "caller":          "mobius_chat",
    }
    threading.Thread(
        target=_post_cache_write, args=(body, ctx.correlation_id), daemon=True,
    ).start()

def _post_cache_write(body, correlation_id):
    try:
        url = os.environ["MOBIUS_CACHE_URL"].rstrip("/") + "/api/skills/v1/cache_write"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "X-Caller": "mobius_chat",
                     "X-Caller-Id": correlation_id},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:
        logger.warning("cache_write fire-and-forget failed (%s); ignoring.", e)
```

The `_should_cache(ctx, payload)` gate is verbatim what's in
`mobius-chat/app/services/cache_writer.py` today — keep it on the
chat side, since it inspects `ctx.retrieval_signals` and
`ctx.cache_influence` which are pipeline-internal.

## What chat removes

| File | After |
|---|---|
| `mobius-chat/app/skills/builtin/cached_answer.py` | Delete. Replaced by HTTP-skill consumer in step 2 above. |
| `mobius-chat/app/services/cache_writer.py` | Delete the Chroma write logic. Keep only `_should_cache(ctx, payload)` (move it inline into `orchestrator.py`'s new `schedule_cache_write_v2`). |
| `CACHE_ASSIST_CHROMA_COLLECTION` env on chat | Remove from `dev.env` + `scripts/deploy.sh`. Only the cache service knows about Chroma. |

## What chat keeps

| File | Why |
|---|---|
| `mobius-chat/app/services/cache_mode.py` | Off / shadow / active mode selector — chat-side policy, not service-side. |
| `CACHE_ASSIST_ENABLED` env | Master switch on chat. When 0, neither the lookup nor the write skill fires. |
| `CACHE_ASSIST_DEFAULT_MAX_AGE_DAYS` env | Per-call default for the lookup body. |
| `_should_cache(ctx, payload)` gate | Inspects pipeline state; stays in chat. |

## Rollout sequence

1. Cache agent ships service Phase 0 (Chroma backend) at
   `https://mobius-answer-cache-...a.run.app`.
2. Chat agent merges the three additions above, with
   `MOBIUS_CACHE_URL=` (empty) initially. Skill returns
   `no_sources` cleanly; cache write is a no-op. Chat behavior
   unchanged.
3. Chat agent sets `MOBIUS_CACHE_URL` to the real URL in dev.env,
   redeploys. Cache lookup + write skills start firing.
4. Chat agent flips `CACHE_ASSIST_ENABLED=1`. Cache is live.
5. Cache agent monitors `/admin/cache_stats` for hit rate. Tune
   `min_similarity` / `max_age_days` defaults from data.

## Backward-compat

The skill name (`cached_answer_lookup`) and inputs schema MUST
match what the planner manifest already advertises. Today's
manifest text comes from `mobius-chat/app/skills/builtin/cached_answer.py`'s
`SkillSpec.description`. Copy it verbatim into the new skill so the
planner doesn't re-learn its tool taxonomy.
