# ReAct + Integrate emission map

All thinking chunks (emissions) the user sees, in order, for the ReAct path (`MOBIUS_USE_REACT=1`).  
Source: `run_react` → `run_integrate` → `format_response`, with `emitter=on_thinking` (orchestrator).

---

## 1. ReAct loop (`app/pipeline/react_loop.py`)

### Pre-loop (optional)

| Condition | Emission |
|-----------|----------|
| Pronoun resolution expanded message | `↺ Understood: <resolved message>` |
| Follow-up to active context (e.g. report) | `◌ Answering from the report we just generated…` |
| Jurisdiction (plan_display) | `✓ Confirmed: <payer>` \| `? Payer not identified — I'll search broadly.` \| `⟳ Payer change: …` \| etc. |

### Always before iterations

- `I'm breaking down your question and choosing the right source…`
- `  (Up to 4 reasoning rounds: each round I decide to use a tool or to give a final answer.)`

### Per reasoning round (1..4)

| Step | Emission |
|------|----------|
| Start of round | `  Round N/4 — <headline>` (scoping → grounding → refinement → finalize) |
| | `  Reasoning round N/4…` |
| After LLM decision | `  → Round N: <thought>` (model JSON `thought` = one-sentence rationale) |

**Router:** each round’s LLM call is logged with `stage=` **`react_1`** … **`react_4`** (same **model pool** as `planner`; Thompson sampling uses **per-round** stats from `llm_calls` / `model_performance_by_stage`).
| If model sets is_complete=true with answer | `  Synthesizing answer…` → exit to integrate |
| If model chooses tool | `  Using <tool>…` |
| If tool is run_credentialing_report | `  (The report runs its own steps below — org, locations, providers, PML, opportunity, etc.)` |

### Tool-specific (from `_execute_tool`)

| Tool | Emission(s) |
|------|--------------|
| refuse | `⊘ <reason>` |
| search_corpus | `◌ Searching our materials…` ; on failure `↓ Not in our materials — will try web next if needed.` |
| google_search | `◌ Searching the web for: <query>…` |
| web_scrape | `◌ Reading page: <domain>…` |
| lookup_npi | `◌ Looking up provider in NPPES registry…` |
| run_credentialing_report | `◌ Running credentialing report (this may take a minute)…` |

### Loop exit

| Case | Emission |
|------|----------|
| Parse error | `  Could not parse model decision — stopping.` |
| Refuse (terminal) | `  Stopping (refuse).` |
| Max iterations, no answer | `  No verified answer after checking materials and web — escalating honestly.` |

**Rule 8 (sufficiency):** When "Recent conversation" is present and the user asks for something the prior answer did *not* provide (e.g. link, URL, specific page), the model must *not* set `is_complete=true` in round 1 — it must call a tool (e.g. google_search or web_scrape) so you see at least two rounds (tool call then synthesize).

---

## 2. Orchestrator before integrate (`app/pipeline/orchestrator.py`)

- `Composing answer…`
- `  (Integrator: turning reasoning + tool output into your answer card.)`

---

## 3. Integrate / responder (`app/stages/integrate.py` → `app/responder/final.py`)

- `  → Building answer card (<mode> mode, score <score>)…`
- `  Draft composer: calling LLM to generate answer card…`
- `  Validator: checking answer card (mode, direct_answer, sections)…`
- [If repair] `  Validator: retrying after JSON repair…`
- `  Final composer: answer card ready.` (on success)

---

## Example: follow-up “get me a link” (with Rule 8)

Expected emissions so the user sees multiple rounds:

1. `I'm breaking down your question and choosing the right source…`
2. `  (Up to 4 reasoning rounds: …)`
3. `  Reasoning round 1/4…`
4. `  → <thought: prior answer didn't include a link, so I'll search>`
5. `  Using google_search…`
6. `◌ Searching the web for: …`
7. `  Reasoning round 2/4…`
8. `  → <thought: found the page, can synthesize>`
9. `  Synthesizing answer…`
10. `Composing answer…`
11. `  (Integrator: turning reasoning + tool output into your answer card.)`
12. `  → Building answer card (…)…`
13. `  Draft composer: …` / `  Validator: …` / `  Final composer: …`

---

## 4. Post-turn quality audit (eval / QC)

Not part of the ReAct loop. After the worker publishes the completed payload, **eval** (`mobius-chat-qa/run_eval.py`) may call **`POST /chat/qc-audit/{correlation_id}`** with adjudication results.

| Mechanism | Behavior |
|-----------|----------|
| **Progress / SSE** | [`publish_quality_audit_event`](../app/storage/progress.py) emits **`event: "quality_audit"`** with `data.line` (human-readable, also merged into `thinking_log`). Redis + `chat_progress_events` when configured. |
| **Poll / completed payload** | [`patch_response_merge`](../app/queue/redis_queue.py) adds **`qc_audit`** `{ passed, reason, source, audited_at }` to the stored response; may add **`usage_breakdown_append`** (adjudicator row) and **`usage_breakdown_enrich`** (`llm_call_id` → `quality_score` / `quality_source` from `llm_calls` after post-run QA). |
| **DB** | [`update_turn_qc_audit`](../app/storage/turns.py) merges into **`chat_turns.qc_audit`** (migration `023_chat_turns_qc_audit.sql`). |
| **UI** | Chat shows the thinking line when the stream is still open; **QC audited** / **QC flagged** badge + delayed refetch if audit lands after `completed`. |

Optional env **`MOBIUS_QC_AUDIT_SECRET`**: when set, the POST must send header **`X-Mobius-QC-Audit-Secret`** (eval and curl must match).
