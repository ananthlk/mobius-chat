# `system_context` — Invocation Spec for Callers

**Status:** live as of 2026-04-22
**Endpoint:** `POST /chat`
**Intended for:** skills / frontends / agents that already hold verified,
structured data and want the chat pipeline to **render or reason about that
data** rather than re-derive it via tools.

---

## Why this field exists

The ReAct pipeline is designed to answer questions by selecting and running
tools (RAG, web search, skills). If a caller prepends ground-truth data into
the `message` string, the planner sees it as **part of the user's query** and
will try to resolve it with tools — wasting LLM calls, introducing latency,
and sometimes overwriting correct values with re-derived ones.

`system_context` solves this cleanly: it's a separate, opt-in field that the
worker treats as **verified ground truth**. The pipeline enters a **Round 0**
short-circuit that tries to answer from the context alone. If that succeeds,
the response is published in ~1 LLM call with no tool invocations. If the
question genuinely needs external data, Round 0 reports `NEEDS_TOOLS` and
the normal Round 1..N loop runs — with the `system_context` visible to
every reasoning round so tools complement rather than contradict what's
already known.

---

## Request schema

```http
POST /chat
Content-Type: application/json
Authorization: Bearer <jwt>   # when CHAT_AUTH_MODE != off
```

```jsonc
{
  "message": "What was BHPF market share in 2019 across service lines?",
  "thread_id": "ch1-profile::rn-market",     // optional
  "chat_mode": "quick",                       // optional: copilot|agentic|quick
  "system_context": "Page: BHPF 2019 Baseline\nPeriod: 2019\nVerified values:\n  bhpf_share_baseline: 0.16\n  bhpf_benes_baseline: 564462\n  mkt_benes_baseline: 2607656"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `message` | string | yes | The user's question — keep it a plain question, not data |
| `thread_id` | string \| null | no | Omit for first turn; server returns one you should reuse |
| `chat_mode` | `"copilot" \| "agentic" \| "quick"` \| null | no | Affects round count; `quick` + `system_context` is the cheapest combo |
| `system_context` | string \| null | no | Pre-loaded ground truth; see below |
| `use_react` | bool \| null | no | Per-request override for ReAct |

### `system_context` value format

**Free-form plain text.** The worker does not parse it — it's handed verbatim
to the LLM inside a labeled block. Keep it focused and machine-clean:

- ✅ **Good:** newline-separated `key: value` pairs, small YAML-like blocks,
  a few short paragraphs of verified prose.
- ⚠️ **OK but watch token budget:** tables, JSON blobs under ~2 KB.
- ❌ **Don't:** dump multi-MB reports, raw CSV of thousands of rows, or
  untrusted user input. The context is passed as-is to the LLM and counts
  against the prompt budget.

**Size guidance:** keep under **4 KB** of text (roughly 1000 tokens). Larger
blobs hit context limits on Flash-class models and undo the latency win.

**Safety:** the string is trusted by the pipeline — do not include
user-supplied text you haven't verified. Prompt-injection attacks inside
`system_context` will succeed because Round 0's instructions say "treat as
verified data." Callers are responsible for sanitization.

### Empty / missing values

All of these produce **identical legacy behavior** (no Round 0, no extra
LLM call, normal ReAct loop):

- Field omitted from the JSON body
- `"system_context": null`
- `"system_context": ""`
- `"system_context": "   \n\t  "` (whitespace-only — normalized to `None` at the worker)

---

## Response behavior

### Successful Round 0 short-circuit

When the context answers the question, the completed response payload
includes a new top-level flag:

```jsonc
{
  "correlation_id": "…",
  "status": "completed",
  "message": "BHPF's 2019 baseline share was 16% (564,462 of 2,607,656 beneficiaries).",
  "sources": [],
  "retrieval_signals": ["system_context"],
  "answered_from_system_context": true,
  "model_used": "gemini-2.5-flash",
  "duration_ms": 850
}
```

- `sources: []` — no citations because no retrieval ran.
- `retrieval_signals: ["system_context"]` — new signal, use this for
  analytics bucketing.
- `answered_from_system_context: true` — stable boolean for UI badges.

### Round 0 falls through to tools

When the LLM returns `NEEDS_TOOLS`, the response looks **exactly like a
normal ReAct turn** — `answered_from_system_context` is **absent** and
`retrieval_signals` reflects whichever path actually answered
(`corpus_only`, `google_only`, etc.). The `system_context` is still
available to every round, so tools complement the pre-loaded data.

### Thinking panel / SSE

Round 0 emits three UI lines when it fires:

```
◌ Checking the pre-loaded context first…
  Answered from pre-loaded context.        (on short-circuit)
  Context insufficient — running full reasoning loop.  (on fallthrough)
```

These stream via `GET /chat/stream/{correlation_id}` like any other
thinking line.

---

## When to use `system_context`

### ✅ Good use cases

- **Story / narrative layers** that click a node with pre-computed metrics
  and want the chat to explain or answer follow-ups about those exact
  values.
- **Skill cards** that already ran a tool and want the chat to answer
  "what does this mean?" questions without re-running it.
- **Dashboard drill-downs** where the user clicks a chart point and asks
  "why is this?" — pass the chart's verified data as context.
- **Workflow forms** where the user fills in fields and asks a summary
  question — pass the filled fields as context.

### ❌ Don't use it for

- **General chat** where the user's question is open-ended. The field adds
  cost; skip it when you don't have pre-verified data.
- **RAG retrieval results.** If you fetched chunks from a document store,
  use the normal RAG pipeline — don't flatten chunks into `system_context`
  because you'll lose citation tracking.
- **Conversation history / memory.** `thread_id` already handles that.
- **User-provided text you haven't verified.** Use the normal message path
  so RAG and adjudication guardrails apply.

---

## Worked examples

### Example 1: Story node click (the primary case)

```javascript
// Frontend (story.html, _buildEnvelope)
const envelope = {
  message: userQuestion,
  thread_id: `ch1-profile::${nodeId}`,
  chat_mode: "quick",
  system_context: [
    `Page: ${node.title}`,
    `Period: ${node.period}`,
    `Scope: ${node.scope}`,
    "Verified values:",
    ...Object.entries(node.values).map(([k, v]) => `  ${k}: ${v}`),
  ].join("\n"),
};
fetch("/chat", { method: "POST", body: JSON.stringify(envelope) });
```

### Example 2: Skill card follow-up

```python
# After a skill runs and has structured output, passing it for follow-ups:
import httpx

skill_output = {
    "provider_name": "Jane Doe, MD",
    "npi": "1234567890",
    "specialty": "Cardiology",
    "status": "active",
    "effective_date": "2023-01-15",
}
context = "\n".join(f"{k}: {v}" for k, v in skill_output.items())

httpx.post("http://localhost:8000/chat", json={
    "message": "Is this provider eligible to bill Medicare?",
    "thread_id": thread_id,
    "system_context": f"Provider lookup result:\n{context}",
})
```

### Example 3: Python SDK style (hypothetical)

```python
from mobius_chat import ChatClient

client = ChatClient(base_url="http://localhost:8000")
resp = client.ask(
    message="Compare these two years.",
    system_context=f"2019 share: 0.16\n2020 share: 0.14",
    thread_id="analysis::bhpf-trend",
)
if resp.answered_from_system_context:
    print("Short-circuit hit — cheap and fast.")
```

---

## Operational notes

### Cost / latency profile

| Path | LLM calls | p50 latency | Tool invocations |
|---|---|---|---|
| Round 0 hit | 1 | ~1s | 0 |
| Round 0 miss → copilot (3 rounds) | ~4 | ~5s | 1–3 |
| No `system_context` (legacy) | ~3–5 | ~5s | 1–3 |

### Analytics

- Filter `chat_turns` rows by `retrieval_signals @> '["system_context"]'` to
  count Round 0 hits.
- `react_rounds_used = 0` on `chat_turns` flags Round 0 short-circuits
  exactly (ReAct rounds are 1-indexed elsewhere).
- The `turn_completed` envelope's `final_signal` field carries
  `system_context` when the short-circuit fired.

### Env flags

No new env flags. The feature is on by default and purely opt-in per
request. If you ever need to force the legacy path for a debugging session,
just omit the field from the POST body.

### Failure modes

| Failure | Behavior |
|---|---|
| LLM call for Round 0 raises | Logged, falls through to normal loop |
| LLM returns empty string | Falls through to normal loop |
| LLM returns `NEEDS_TOOLS` sentinel | Falls through; `system_context` is still visible to rounds |
| `system_context` exceeds prompt budget | LLM may truncate; consider shrinking your context |

---

## Contract guarantees (what won't change)

- Field name: `system_context` (snake_case, string type).
- Sentinel value: the literal token `NEEDS_TOOLS`.
- Response flag: `answered_from_system_context: true` on short-circuit,
  **absent** otherwise (not `false` — absence is the signal).
- Retrieval signal string: `"system_context"` (matches
  `RETRIEVAL_SIGNAL_SYSTEM_CONTEXT` on the server).
- Omitting the field is equivalent to legacy behavior forever.

If any of these change, the change will be documented in this file and the
old spelling will be accepted for at least one minor version.

---

## Implementation references (for maintainers)

- `app/api/chat.py` — `ChatRequest.system_context`, payload forwarding
- `app/worker/run.py` — `process_one` unpacks + normalizes
- `app/pipeline/orchestrator.py` — `run_pipeline(..., system_context=...)`,
  envelope flag injection
- `app/pipeline/context.py` — `PipelineContext.system_context` field
- `app/pipeline/react/round0.py` — `try_system_context_round0()`, prompt
  construction, sentinel detection
- `app/pipeline/react_loop.py` — Round 0 invocation + per-round context
  prefix on fallthrough
- `app/services/doc_assembly.py` — `RETRIEVAL_SIGNAL_SYSTEM_CONTEXT` constant
- `tests/test_system_context.py` — 27 tests covering the full flow
