# SPEC — Gap → Strategy → Tool: a persistent evidence ledger for React's reasoning loop

**Author:** ReAct Agent
**Status:** DRAFT — proposed, not built. Flagging for Chat Architecture review before implementation, per this file's ownership area's standing rule (no code changes to `react_loop.py`/`react/prompts.py` without sign-off).
**Origin:** Ananth, live in this session, reacting to a real production trace (see §1) — "this is because it is not learning really from what it learnt... i have this answer, i have these gaps, let me fill my gaps... keep a list of what it has learnt and gaps in its understanding and continue to develop a strategy (tool, query rewrite) — may be it should choose this gap >> strategy >> tool logic." Explicitly corrected a first-draft framing of mine that conflated this with prescribing tool *order* — "no it is a structural change and not a sequence of tools as we cannot control that." The tool the LLM picks next should fall out of better reasoning against real state, not a hardcoded sequence.

---

## 1. The gap this addresses — with live evidence, not just theory

The 2026-08-06 P0 fix (commit `4268736`) replaced the dead "5-arm bandit" with a real, bounded 3-call citable_required relax-then-reframe protocol, and added rule 1c requiring each round's `thought` to show LEARNED → RESTRATEGIZE → NEXT. That fix is live and working as scoped — but a live re-test the same day exposed the deeper problem it doesn't solve.

Query: *"Is prior authorization required for H0031 documentation under Molina Healthcare Florida Medicaid?"* (agentic mode, correlation_id `4ffd387c-e7e7-420d-b46d-63d6da378315`, rev `mobius-chat-00677-hrp`). Exact queries sent to `rag`, pulled from the live `retrieval_trace` telemetry:

| Round | Query sent to rag |
|---|---|
| 1 | `"Molina Healthcare Florida Medicaid prior authorization H0031"` |
| 2 | `"Molina Healthcare Florida Medicaid prior authorization requirements for HCPCS code H0031"` |
| 3 | `"Molina Healthcare Florida Medicaid prior authorization requirements for HCPCS code H0031"` |

Round 3 sent the **byte-for-byte identical string** as round 2 — not a reword, a duplicate. Yet round 3's own `thought` field claims: *"I need to make a more targeted query to find this information."* That statement is false; nothing changed. Rule 1c's "if your thought could be pasted onto a different round, you haven't reasoned from the result" instruction did not prevent this — it's a prompt-level ask with no code enforcement, and the model didn't comply.

Separately: round 4 called `healthcare_query`, which is where the turn actually learned something new — *"H0031 is HCPCS Level II, community support service line."* But the rag budget (3 calls) was already spent by round 3, so that new fact only fed the final give-up answer, never a reframed rag query. The turn ended with an honest "not found," even though a genuinely better rag query (targeting "community support service line PA requirements" instead of the bare code "H0031," which likely never appears verbatim in the corpus) was never attempted.

**Root cause:** the reasoning context each round is a fresh reaction to the *single last tool result*. There is no persistent state carrying forward "here's what's confirmed, here's what's still an open gap, here's which gap this next call is aimed at closing." The model re-derives its whole understanding of the question from scratch every round, so instructions like "make it materially different" or "use what you learned" have nothing durable to act on — they're asking the model to remember something it was never given a place to write down.

This is bigger than retrieval. `rule 1b`'s citable_required protocol is a real, working special case of exactly this problem, hand-built for one tool (`rag`) and one specific gap-type (citability gating). The general problem — "what do I know, what's missing, what's my plan to close it" — applies to every tool in the manifest, not just rag.

## 2. Proposed structure (sharpened by Eval-architect + Product Promise, 2026-08-06 — see §4)

A persistent, per-turn ledger, updated after every tool call regardless of which tool ran:

```
EvidenceLedger:
  learned: [
    { fact: <str>, source_round: <int>, source_tool: <str>, open: <bool> }
  ]
  open_gaps: [
    { gap: <str — specific, not "need more info">, status: "open" | "closed", closed_by: <round, if closed> }
  ]
  gap_progress: "shrinking" | "stuck" | None   # derived, not authored — see §2.1
```

**No confidence field on facts** — Eval-architect's ruling (2026-08-06), and it's load-bearing, not a style choice: a self-assessed confidence on a learned fact would be the model grading its own output, exactly the self-report pattern Eval's quality line refuses to trust ("a hallucinating round is maximally self-confident" — same failure mode as trusting `decision.confidence` for completion, which the existing groundedness floor already doesn't). Facts carry **provenance** (which round, which tool) instead — cheap, structural, useful for dedup and "have I already tried this," and not a quality claim. The external quality grade stays Eval's job, post-turn, on the actual output — a separate axis from this loop-internal state.

### 2.1 `gap_progress` — the Product Promise interface

Product Promise (2026-08-06) confirmed they want gap-closure as their round-extension signal — a strict upgrade over their current rule 5, which gates on `self_reported_confidence != high` (the same self-report problem Eval flagged above, in a different spot). Their explicit interface ask: **the ledger does the summarizing, the governor stays a pure function over simple scalar/enum inputs** — same pattern as `groundedness_passed`/`elapsed_s` today. So the ledger doesn't hand the governor `learned`/`open_gaps` to parse; it exposes one derived field:

```
gap_progress: "shrinking"  — at least one gap closed since the last round
             | "stuck"     — N consecutive rounds, zero gap closures
             | None        — not enough rounds yet to judge
```

Concrete consumers Product Promise specified, to fold into `governor.py::evaluate()` once this exists:
- **Rule 5 (extend-on-exhausted-budget)** gains a condition: `gap_progress != "stuck"`. Don't grant a bonus round to a demonstrably stalled line of inquiry regardless of what self-reported confidence claims — this is precisely the round-3-Molina failure mode (thought claims progress, ledger would mechanically show zero closures).
- **New early-consolidate trigger**: "stuck for N consecutive rounds" can justify moving to consolidate *before* `soft_target_s` is reached — continuing to search when gaps aren't closing wastes budget even with wall-clock time left. This is new; today's governor only looks at time/round-count, not progress velocity.

This resolves what was an open question in the original draft (§4 below) — the ledger and Product Promise are not two competing "should we continue" systems. The ledger produces one honest scalar; `governor.py::evaluate()` remains the single place that turns it into a directive.

### 2.2 Efficiency/cost telemetry — a second consumer, orthogonal to quality

Eval-architect's second point: gap-closure data IS worth consuming, but as **efficiency/process telemetry**, never as `quality_score`. The two axes are orthogonal — a turn can be efficient-but-wrong (closed every self-defined gap, still hallucinated) or wasteful-but-right (ground through 3 redundant rounds, landed a good answer). Gaps are self-defined, so gap-closure can never be the quality reward. But the *objective* part — "3 rag calls on byte-identical queries" is waste regardless of any self-assessment — maps cleanly onto the bandit's **cost** dimension. Proposed emission: `redundant_calls` / `rounds_without_gap_closure` as cost-side telemetry, kept separate from the quality term. Loop Eval in when this shape firms up so the names line up with what the bandit's cost dimension already reads.

Each round, the reasoning prompt renders the current ledger (not just the last tool result), and the decision JSON is extended to require the model to reason in three explicit, separately-inspectable fields — not folded into one free-text `thought`:

```
{
  "gap": "<the ONE open gap this round is targeting, quoted from open_gaps or newly identified>",
  "strategy": "<the approach to close it — e.g. 'reformulate around the service-line classification instead of the bare code', 'relax a constraint', 'try a tool that can classify the code first', 'accept as a genuine gap and stop'>",
  "tool": "<the concrete tool + inputs implementing that strategy — or null if the strategy is 'stop and answer'>",
  "inputs": {...},
  "is_complete": false
}
```

After the tool result comes back, a ledger-update step (LLM-authored, or code-derived where the tool's own telemetry makes it mechanical — e.g., rag's `status`/`n_chunks` already tells you whether a gap closed) appends to `learned`/`open_gaps` before the next round renders it. This is what makes "the query changed because of what was learned" checkable — not by reading a thought sentence and hoping, but by diffing the ledger.

The citable_required relax-then-reframe protocol shipped today becomes a special case: "citable_required=True + 0 chunks" is one specific, mechanically-detectable *gap* ("no citable evidence exists yet"), and "relax → learn → reframe" is one specific *strategy* for closing it. The general ledger doesn't replace that protocol; the protocol becomes one strategy the general reasoning loop can choose among.

## 3. What this deliberately does NOT prescribe

Per Ananth's correction: this is not a tool-ordering mechanism. It does not say "run healthcare_query before rag" or encode any fixed sequence. The model still picks the tool each round; the change is that it picks against a real, accumulated understanding of what's known and what's missing, instead of the last single result in isolation. Better sequencing (e.g., classifying a code before searching for it) should fall out of the model reasoning well against the ledger, not from us hardcoding an order — hardcoding it would just be a different flavor of the same rigidity this spec is trying to remove.

**Bonus benefit, flagged by Eval-architect, not the original motivation but worth stating:** splitting each round's decision into explicit `gap`/`strategy`/`tool` fields instead of one free-text `thought` also makes each round independently gradeable — "did this round pick the right tool for the stated gap" becomes a per-round signal, which lines up directly with Eval's existing per-stage reward-attribution work (each react round graded on its own output, not just the turn as a whole). The schema change earns its cost twice: once for the LLM's own reasoning discipline (§1), once for making the loop's rounds legible to Eval in a way free-text never was.

## 4. Open questions

Two of the four questions from the original draft are now resolved by direct owner input (2026-08-06) — kept here for the record, folded into §2.1/§2.2 above:

- ~~Interaction with Product Promise~~ — **RESOLVED.** Product Promise confirmed `gap_progress` becomes their actual extension-trigger input (§2.1), not a second competing "should we continue" system. `governor.py::evaluate()` stays the single place that turns signals into directives; the ledger just adds one more honest scalar input, same shape as `groundedness_passed` today.
- ~~Fact schema (confidence?)~~ — **RESOLVED.** Eval-architect ruled confidence-on-facts is a self-report trap; facts carry provenance (`source_round`/`source_tool`) instead (§2). Gap-closure data feeds Eval as cost/efficiency telemetry, never as `quality_score` (§2.2).

Still genuinely open — not deciding these alone:

- **Ledger authorship.** Does the LLM rewrite the whole `learned`/`open_gaps` lists each round (simpler, but costs tokens and risks the model silently dropping a gap it didn't mean to close), or does code do additive/mechanical updates from tool telemetry where possible (cheaper, more reliable for tools with structured output like rag, but needs per-tool logic for tools with unstructured output)? Likely a hybrid — code closes gaps it can prove closed from telemetry (e.g., rag's real `status`/`n_chunks`), LLM proposes new gaps and non-mechanical closures. This also determines who computes `gap_progress` (§2.1) — probably code, mechanically, from the same telemetry that closes gaps, so it's as self-report-proof as the facts themselves.
- **Token cost.** A growing ledger rendered every round is real, compounding token cost on long agentic turns (up to 10 rounds). Needs a cap/summarization strategy (e.g., only show open gaps in full, collapse closed ones to a one-line history) — same shape of problem `previous_thread_summary` already solves for conversation history, possibly reusable.
- **Schema enforcement.** Splitting `thought` into `gap`/`strategy`/`tool` is a decision-JSON contract change — touches `_parse_react_decision_json` and every consumer of `decision.get("thought")` (the emitted "Round N: ..." line, `thinking_log`, the critic's context-building, and now Eval's per-stage attribution per §3's bonus point). Needs an inventory of what breaks, same discipline as any schema change in this file's ownership area.
- **Does this replace or wrap rule 1b/1c?** Rule 1c's LEARNED/RESTRATEGIZE/NEXT was a first attempt at this same goal, shipped as prompt-only text with no persistent state and no enforcement — today's evidence shows it isn't sufficient alone. This spec doesn't repeal it; it gives it something real to reason against instead of asking it to hold state in a single free-text sentence.

## 5. Not scoped yet

Implementation plan, exact ledger cap sizes, and the parse-schema migration are intentionally not detailed here — this is the design-level proposal for Chat Architecture to rule on the open questions in §4 before any code gets written, matching the process the citable_required protocol went through before it shipped.
