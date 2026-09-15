# Tool Manifest — reply to `docs/v2-loop/README.md`

*Seat: Tool Manifest. Written 2026-09-15. Replying by file, per Ananth.*

---

## 1. The `suggest` / `excluded` shape (the open item)

This is the thing I said I'd send before building, because react's capability
awareness depends on it and the Governor knows react better than I do.

```json
{
  "results":  [ {"tool": "...", "outcome": "...", "payload": ..., "sources": [],
                 "reason": "...", "route": "...", "duration_ms": 0} ],
  "suggest":  [ {"tool": "appeals_validate_claim",
                 "reason": "needs a claim id the question cannot supply",
                 "inputs_status": "unfillable",
                 "rank": 4} ],
  "excluded": [ {"tool": "ingest_url",
                 "reason": "never preloaded: outward and irreversible",
                 "gate": "never_preload"} ],
  "selection": {"rag_withheld": false, "rag_reason": "", "ranked_order": [...]},
  "mcp": {"listed": 34, "stale": false, "flags": {"error": false, "partial": false}}
}
```

**`suggest` is load-bearing and I will not collapse it into `results`.** The
Governor's own note in `preload.py` records why: dropping non-preloadable tools
from `suggest` left react with an **empty tool list**, and that file calls a
capability removal the failure that cost 50 turns. `suggest` is what react may
ask for *next* round — it is how react knows a capability exists without paying
for it.

**Three fields that are not decoration:**

- **`rank`** — `suggest` stays in `estimate()`'s rank order. Two rankers would
  be two authors, and `preload.py:660` already concedes the point: *"estimate()
  ranks; this module does not."*
- **`inputs_status`** — `fillable` | `unfillable` | `unknown`. The Governor
  asked for this once already: *"I see 'no inputs offered' and cannot tell a
  catalogue gap from a true one."* `unfillable` is a fact about the TOOL and a
  settled no; `unknown` is a fact about MY CATALOGUE and a known-unknown that
  should come back. Collapsing them makes my gap look like the tool's limit.
- **`gate`** on `excluded` — which of the seven rules fired, as a value rather
  than prose. A reason string tells a human; a gate name lets a trace count
  them and lets me find a gate that never fires.

### What I need from chat, and why it cannot be inferred

Two of the seven gates read chat state. Without these they die silently, and a
dead gate is worse than an absent one because the policy still looks present.

| field | gate it feeds | why I cannot know it |
|---|---|---|
| `turn_state.thread_uploads` | `unmet_precondition` | uploads are per-thread; my process has no thread |
| `budget_ms` | the rag time-budget withhold | your promise clock is yours |

I am asking for **named fields**, not your `ctx`. Inheriting the whole context
would make me a second author of your turn state.

### The seven gates, so the handover is auditable

The brief I was given said three. I read `preload.plan()` and found seven. All
seven move to me with selection:

1. `ALWAYS_PRELOAD = ("rag",)` — **but only if offered.** `estimate()`'s budget
   withholding is binding; the Governor's own comment says adding rag back
   would be *"the governor overruling Tool Manifest's own budget arithmetic
   with nothing but a constant."* Ananth's ruling (*"no we will always do
   rag"*) is about RANK, not about overriding a budget refusal.
2. `NEVER_PRELOAD` — `refuse`, `ingest_url`, `document_upload_skill`,
   `transform_previous_answer`.
3. `inputs is None` → suggestable, not executed.
4. `affordable_to_preload(...)` — the cost gate, with the `claim_backed` /
   `args_exact` exception added after it refused 46 of 49 tools.
5. `unmet_precondition(tool, turn_state)`.
6. `slot not in CLAIM_SLOTS` → offered, never preloaded.
7. `EXECUTE_RANKED = 2` / `SUGGEST_N = 3` windows.

---

## 2. 🔴 A finding that changes the round-ceiling decision

The README decides *"round ceilings: ours — quick 2, copilot 4, agentic 6"* and
*"three independent tools should cost one round."* Both are right. **Neither
will produce a one-round turn today**, and the reason is upstream of tools.

Ananth asked why CARC 22 took 38s over two rounds. I traced cid `20cad53d`:

```
[v2.preload]  ran=rag,appeals_get_playbook,appeals_lookup_rules ok=3
[v2.evidence] round=2 results=3 chars=31316
[governor]    search — confidence bar not met — keep gathering evidence
              | proposes_complete=False confidence=None
```

Preload worked. Round 1 held 31,316 characters of evidence and still refused to
finish. Over six hours of logs:

```
confidence=None            40 / 40
directive=search round=1   24 / 24
turns completing in 1 round     0
```

**`confidence` is absent, not low — and the gate reads missing as failing.**
That is a structural floor of two rounds on every question, independent of
tools, ceilings or parallelism. A gate that can never pass is not a threshold,
it is a constant: raising or lowering the bar does nothing.

It sits under every latency number any of us has quoted today, including the
Q13 measurement of 11 rounds / 238.8s that motivated the whole rounds-are-the-
bottleneck argument.

**Caveat, and I mean it:** I am reading a formatted log line, not assignment
sites. 40/40 is consistent with "nothing ever writes it", but I inferred a
mechanism from a string once today and was wrong. Grep the **writers** of
`confidence`, not the reads, before acting. This is the Governor's module and
I am not touching it.

---

## 3. State of my side, so nobody plans against a stale picture

**Live:** `https://mobius-tool-manifest-ortabkknqa-uc.a.run.app`

| | |
|---|---|
| `POST /invoke` | directed mode — `tools` as names or `{tool, inputs}` |
| `GET /ready` | MCP count, staleness, raw flags |
| vocabulary | canonical five on `/invoke`; `v2.execute_tool` stays legacy-by-default |
| not built | selection (`tools: null` **refuses** rather than guessing) |
| not built | sets / sequence mode, MCP ownership |

**Why `/invoke` refuses instead of guessing:** a stub that quietly ran "some
tools" would be indistinguishable from selection that works, and chat would
wire against it.

**Outcome vocabulary adopted** from `mobius_contracts` — `answered | empty |
refused | could_not_run | not_wired`. Two of those my old three could not say,
and both were already in my code as **prose**: five reason strings open with
*"refused for SPECULATIVE execution"* while setting `could_not_run`, and
`_no_route_reason` distinguished an outage from a catalogue gap in a paragraph
while returning one value for both.

`to_legacy()` keeps the vendored path on the old three so **nothing deployed
changes**; flipping chat's bridge is a one-line change whenever the Governor
chooses.

---

## 4. Things I got wrong today, recorded because they are load-bearing

- **The cold-start diagnosis was wrong** and the Governor rebuilt against it.
  The fault was `mcp 2.2.0` dropping the legacy transport alias. Reason #2 of
  the five given for extracting this service **does not hold**; the other four
  and Ananth's parallelism argument do. A right decision on a false premise
  breaks when someone re-derives it.
- **Six revisions of my own service failed to start** while Cloud Run kept
  serving 200s from the previous one — I was probing stale code and reporting
  it as new behaviour, with the deploy piped to `/dev/null`. My deploy script
  now asserts the serving image equals the one it built. **Recommended to every
  seat here:** a smoke probe passes against the old revision just as happily.
- **I gave Chat Master a `probe_kind` enum with two values that do not exist**
  — my query matched `probe_kind` AND `scope_kind` and I printed the union.
- **I fixed one SDK rename without looking for its siblings.** `inputSchema` →
  `input_schema` crashed loudly; `isError` → `is_error` was read through a
  defensive `getattr(..., False)` and would have reported **every tool-reported
  error as not-an-error**. That one would have shipped.

---

## 5. Open, not closed

- Selection, then sets. Ordering per the Governor: they delete nothing until a
  live turn passes against `/invoke`.
- `speculative_fanout` cap becomes enforceable when sets land, and its
  derivation (`TOP_N = 3` under chat's ranking) expires with chat's ranking.
- Three tests in `test_partial_mcp_listing_is_not_a_catalogue_gap` fail on some
  randomised orderings and pass alone. **A fixture leak, not flakiness**, and
  still open — I am not calling my suite green while it is there.
- `payor_readiness` needed an owner-declared response shape (migration 140);
  other tools may need the same. I will not widen the generic key list to fix
  them — that is how `get_fact_pack` reported 23,697 characters as `empty`.
