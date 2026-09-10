# MCP analytics tools: three findings from the market-size probe

**Author:** Payor Policy Agent · **Date:** 2026-09-10
**Measured against:** dev rev `00977-8bl` (digest `sha256:ca0695e4…9327a`, commit `299519c`)
**Status:** findings only. No production code changed. Tests added (characterization, passing).

Product Awareness asked for one thing: *"Is any `get_*` tool emitted at all?
If none is emitted on a turn squarely in their domain, the finding hardens
considerably."* Their caveat was that a negative has two causes — not selected,
or the query didn't read as market-analytics to the planner.

The answer is not negative, and a third cause appeared that neither of us listed.

---

## Finding 1 — the "29 never-emitted MCP tools" hypothesis is RETIRED

The tools are selected, dispatched, and return real data. Three turns on
`00977-8bl`, all confirmed in `turn_spans`:

| Query | Emitted | Dispatched | `tool.result` |
|---|---|---|---|
| market size for behavioral health | `get_market_size` | `success` | `no_sources` |
| …in Florida | `get_market_size` | `success` | `no_sources` |
| top organizations by market share | `get_top_orgs`, `get_market_share_timeseries` | `success` | `no_sources` |

58 tools offered per turn, 25 of them `get_*`. Selection is correct each time —
`get_top_orgs` **and** `get_market_share_timeseries` together for a market-share
question. The answers carry real figures (2,923,378 beneficiaries;
$793,099,275.81 paid; named organizations with revenue).

They had never been emitted because **nobody had asked a market-analytics
question**, not because the planner couldn't pick them. This is the same shape as
the payor-name hypothesis: an absence in telemetry that measured the query mix,
not the capability. Ask the question, the tool fires.

## Finding 2 — `signal` is single-valued across success and failure (LIVE DEFECT)

`app/skills/mcp_adapter.py` — the success branch attaches a `SourceRef` and, in
the same envelope, sets `signal="no_sources"`. The failure branch sets the
identical value. One value, both branches. A field that cannot take a second
value cannot discriminate, and it is read by consumers that assume it can:

1. **`_skill_golden`** (`react_loop.py:2813`) requires
   `signal not in ("", RETRIEVAL_SIGNAL_NO_SOURCES)`. A successful MCP analytics
   answer is therefore **never authoritative** — the golden early-exit never
   fires, and the loop stays free to escalate to `google_search`, which anchors
   composition on web content instead of the figures the tool returned.
2. **`final_signal`** (`react_loop.py:6099`) only updates when the signal is not
   `no_sources`. The badge reads *no sources* on a turn that returned real rows.
   Observed: `citations: []` on the market-size answer despite a SourceRef.
3. **`tool_result_verdict`** (mine) reports `no_sources` for every MCP call
   either way. **Funnel stage 3 is blind across the entire MCP surface** — a
   populated analytics answer and a dead MCP server produce the identical span.
   Verified directly, not inferred:
   ```
   success payload verdict : no_sources
   failure payload verdict : no_sources
   ```

**`ReactRetryGuard._is_zero_result` is NOT affected** — checked, not assumed. It
requires `no_sources` **and** an empty sources list, and the success branch
attaches one source. That single `SourceRef` is the only thing preventing
successful analytics calls from incrementing `consecutive_failures_per_tool` and
tripping tool-exhaustion. Whoever fixes this must not remove it before fixing the
signal, or a working tool starts getting blocked for succeeding.

**The existing suite pins the defect as intended behaviour.**
`test_success_returns_envelope_with_mcp_source` and
`test_failure_returns_error_envelope_not_raise` both assert
`env.signal == "no_sources"` — the same assertion on opposite branches, green.
That is the tell for a single-valued field, and it is why this survived.

Added `TestMcpSignalIsSingleValued` (3 tests, passing) to characterize it. They
document rather than fix, and will fail loudly when the adapter is corrected —
that is their purpose. **Not fixed**: this is the tool surface, sequenced behind
Ananth's schematic.

## Finding 3 — the PHI message gate blocks any city name (HANDOFF, not mine)

The original probe never ran. `"what's the market size for behavioral health in
Tampa"` returned **HTTP 422**, `identifier_labels: ["Address"]`, offset 48
length 5 — the word **"Tampa"**.

Isolated with a control set:

| Query | Result |
|---|---|
| …behavioral health in **Tampa** | 422 blocked — Address |
| …behavioral health in **Miami** | 422 blocked — Address |
| …behavioral health (no geography) | 200 |
| …behavioral health in **Florida** | 200 |
| top organizations by market share | 200 |

Any city blocks; state, no-geography and org phrasings pass. A market-size
business question is unanswerable if it names a city — squarely the
name/address-only false-positive class the `overridable` design targets, and it
is on the **message** gate (`api/chat.py:247`), not the upload gate.

Filed for the PHI seat. **I have not touched it** — per Ananth's standing
instruction that PHI pairs with Browser Extension.

## Also observed, not diagnosed

The market-size answer's own `confidence_note` reads: *"The provided `rag_chunks`
only contains partial 2018 data … and does not support the 2024 market size
figures presented in the answer."* — with `citations: []`. The figures almost
certainly came from the MCP tool rather than being invented, and Finding 2 is why
they could not be cited. Worth a look by whoever owns composition: the note is a
producer, and I have not established that anything renders it.

The top-orgs answer lists `MONTEFIORE MEDICAL CENTER` and `UNIV. OF ROCHSTRONG
HOSP` — New York providers — in a Florida Medicaid result. Data-integrity signal,
not mine.

## What I recommend, in order

1. **Finding 2 first.** It is small, contained, and it is currently discarding
   good analytics answers in favour of web search. It also unblinds the tool
   funnel, which everything else is being measured with.
2. Finding 3 to the PHI seat as a live instance of the `overridable` class.
3. Finding 1 closes the MCP-selection question. No further probe needed.
