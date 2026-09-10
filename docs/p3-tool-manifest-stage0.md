# `tool_manifest` — Stage 0 report

**Verdict: Stage 0 CANNOT name the layer, and the reason is the finding.**
Reporting and stopping, per the work order's own instruction. No fix written.

**Window** 2026-09-09 14:17:56Z → 2026-09-10 01:03:14Z (~10.75h) ·
**78 distinct turns** with spans, dev. Counts are absolute; `n` is span count
entries, `turns` is `COUNT(DISTINCT correlation_id)`.

---

## The funnel — two of four stages are unmeasurable

| # | Stage | Measured? | Result |
|---|---|---|---|
| 1 | `tools_offered` | **partially** | 76 turns, 167 records — **every one `__unfiltered__`**. Zero per-tool records. |
| 2 | `tools_called` | **ambiguous** | 56 turns. 86 real emissions across 11 tools; `__none__` on 11 turns. |
| 3 | `tools_returned_nonempty` | **NO** | `tool.dispatched` is 86 records / 56 turns (76 success, 10 failure) — but `success` means the tool's own flag, not non-empty. |
| 4 | `answer_claimed_absence` | **NO counter** | Derived by reading `chat_turns.final_message` by hand. |

### What the measurable part does establish

**The emit→dispatch boundary is clean: 86 emitted = 86 dispatched.** Every
parseable tool call in the window was executed. **"The model asked and the
executor never ran it" is eliminated** — the one layer boundary the instrument
covers end to end shows zero loss.

**Subscription/policy narrowing is eliminated too:** `ctx.allowed_tools` was
`None` on 76 of 76 turns, i.e. no filter from `user_tool_subscriptions` or
request policy on any turn in the window.

### What it cannot establish, and why each gap is structural

**L1 — `__unfiltered__` records the FILTER, not the MANIFEST.**
`orchestrator.py:745-756` records per-tool names only when `allowed_tools` is a
list; when it is `None` it records the single sentinel. `None` means "no filter;
all *mode-appropriate* tools" — so mode still governs the manifest's contents,
and nothing records what those contents were. We can say no tool was *filtered
out*. We cannot say any given tool was *offered*.

**L2 — `__none__` conflates "chose nothing" with "emitted malformed JSON".**
`react_loop.py:4907-4914` records `__none__` when the parsed decision carries no
tool. A malformed emission fails in `parsing._parse_react_decision_json`, which
returns `None` (`parsing.py:121, 136`) or logs at **debug** (`:110`) — no span,
no persisted count. Both paths arrive at the same `__none__`.
The code comment already says it: *"a drop with no record is indistinguishable
from the planner never emitting a tool at all."*
**So Ananth's "json formatting" hypothesis is currently untestable.** The one
parse-failure `logger.warning` (`parsing.py:132`) fired **0 times in 12h** — but
it covers only the late balanced-extract path; the earlier paths are silent, so
zero warnings is not evidence of zero parse failures.

**L3 — the tool payload is not persisted at all.** See below.

---

## The traced instance — reproduced, then blocked

**`143309c1-4e3a-43b1-a1e0-9ab1c9535d89`** (2026-09-09 18:44:16Z) is the reported
bug, on persisted evidence:

- span: `tool.dispatched = appeals_get_playbook:success`
- `chat_turns.final_message`: *"It looks like our resources **don't have specific
  instructions** for a CARC 24 denial, but I can walk you through the general
  appeal process."*

The tool was dispatched and reported success; the answer asserts absence.

**The trace stops there, because the payload is not stored.**

`chat_tool_results` exists (`db/schema/019`), is keyed `(thread_id, tool_hint)`,
and holds **0 rows in the entire dev database**. Its module `app/storage/results.py`
provides `save_tool_result`, `get_tool_result`, `format_cached_result` and
`clear_tool_results`. External callers:

| function | callers |
|---|---:|
| `save_tool_result` (writer) | **0** |
| `get_tool_result` (reader) | **0** |
| `format_cached_result` | **0** |
| `clear_tool_results` (DELETE) | **1** — `state_load.py:130` |

**The only live consumer of the tool-result store is the one that empties it.**
A table written by nothing, read by nothing, and cleared on every STANDALONE
turn. This is the origin class of the whole program — a producer that never
existed, with a consumer that has a plausible default — and it sits directly
between the two layers Stage 0 was asked to separate.

Because of it, for turn `143309c1` we cannot answer the question that decides the
layer: **did `appeals_get_playbook` return a real playbook that the answer
ignored (L3), or return `success` with an empty payload, making the answer
correct?** `success` is `result.get("success")` (`react_loop.py:5777`) — it does
not imply content.

---

## What this changes about the work order

1. **The layer cannot be named from current evidence.** Not "I could not find
   it" — the distinguishing record does not exist. Two of the funnel's four
   stages have no instrument, and a third is ambiguous by construction.
2. **The next change is instrumentation, not a fix**, and it is small:
   record per-tool names when the manifest is unfiltered (or record the manifest
   itself); split `__none__` into `__none__` vs `__unparseable__`; record
   dispatch result *emptiness* alongside its success flag. Each is a count, at a
   site that already records a neighbouring count.
3. **A speculative fix now would be unattributable** — which is what the work
   order forbids, and the reason it asked for Stage 0.

## `make_tool_failed`

Left alone, and saying so rather than leaving it unmentioned. It has zero
callers, so `tool_failed` is structurally impossible — but Stage 0 did not land
in L3, so per the work order it is out of scope for this pass. Noting that the
`chat_tool_results` finding above is the same shape and probably the same fix
conversation.

## Not done, deliberately

No fix. No instrumentation added yet. No second contract tag (held pending
Eval's ruling on tag shape). `react_loop.py` untouched.
