# ReAct completion-gate critic — design (Task #104)

Status: **DRAFT — awaiting sign-off before implementation.**

## 1. What this is

A single critic call at the point the ReAct loop is about to accept
`is_complete=true` and finalize, in **chat.thinking mode only** (today's
`chat_mode == "agentic"` — see open question §6.2 on whether #103 renames
this). It checks the proposed answer against the *original question's
sub-parts*, not against citations/groundedness (that's the existing
Product Promise floor's job, unchanged).

- **satisfied=true** → fall through to existing finalize path unchanged.
- **satisfied=false**, budget allows another round → inject `uncovered` +
  `suggested_next_query` into the next round's context and loop back.
- **Hard exit**: if no budget remains for another round, complete anyway
  regardless of verdict — this gate never blocks a turn from finishing.

Not per-round. Runs once per *completion attempt* (i.e., it can fire more
than once per turn if round 2's attempt is also found incomplete and
round 3 tries again — bounded by the extension budget, see §3).

## 2. Where it hooks in

`app/pipeline/react_loop.py`, inside the `if is_complete or not tool:`
block, inside `if answer:` (~line 4719) — **the exact same trigger point**
the Product Promise groundedness floor already uses for its own
extend-or-finalize decision (lines 4719–4838). This is not a new
interception point; it's a sibling check at one already established.

**Ordering: this gate runs BEFORE the groundedness floor, not after.**
Rationale: the groundedness floor does per-claim verification against
retrieved chunks — expensive, and pointless to run on an answer that's
about to be thrown away for another round because it never addressed
Aetna at all. Cheaper/coarser coverage check first, expensive precision
check second, only on an answer that's actually structurally complete.

```
if answer:
    # NEW: completion-gate critic (chat.thinking only) — §2
    if <mode is chat.thinking> and rn < max_it:
        verdict = run_completion_critic(...)
        if not verdict.satisfied and <extension budget available>:
            ctx.completion_critic_gaps = verdict.uncovered
            ctx.completion_critic_next_query = verdict.suggested_next_query
            max_it += 1  # bounded, see §3
            tool_results.append({...})  # observation marker, same pattern
            continue                     # back to top of round loop
    # existing Product Promise groundedness floor, unchanged
    if _pp_enabled and ...:
        ...
```

Skipped entirely when `rn >= max_it` — no round left to loop back into,
so running it would just burn a call for a verdict that can't act. This
also means it never fires on the actual last round; that answer ships
as-is (existing `incomplete_coverage`/`gaps_open`-in-prose handling at
line 4704–4707 already covers "shipped anyway with a known gap").

## 3. Extension budget — reuse the governor's, don't add a second one

`governor.py` already tracks `ProductPromiseContract.max_extension_rounds`
per mode (agentic: 3 today) and `_pp_extension_rounds_used` is already a
live counter in the round loop (line ~4642, used by the groundedness
floor's own `directive == "extend"` branch at line 4809-4817).

**Recommendation: share this same counter/ceiling**, not a second
independent one. Two independent extension budgets (one for groundedness,
one for coverage) could compound to more total rounds than the mode's
contract intends, defeating the point of having a ceiling at all. If the
completion-gate grants an extension, it increments the *same*
`_pp_extension_rounds_used` the groundedness floor checks — either gate
running out of budget first still caps total extensions at
`max_extension_rounds`.

This is an explicit design choice to flag for sign-off, not a foregone
conclusion — see open question §6.1 for the alternative (separate pools).

## 4. The critic prompt

Modeled on `integrator_critic`'s compact-JSON-critique style
(`app/responder/final_parallel.py`'s Call B: fast model, ~2s latency
budget, structured output) rather than react's own groundedness
`critic.py` (which is per-claim/per-citation, a different and heavier
job). New system prompt, new function — not a call into either existing
critic path.

**Input:** original question (`ctx.effective_message or ctx.message`) +
the round's proposed `answer` text. Deliberately **not** the full source
pool or tool_results — keeps the call cheap and fast (see §6.3 for why
this is a real tradeoff, not a given).

**System prompt sketch:**
```
You check whether an answer actually covers everything a question asked
for — not whether it's well-cited (a separate check handles that).

Look for:
1. Every named entity/category in the question (e.g. "for each of:
   primary care, BH, SUD, FQHC" — did the answer address all four, or
   only some?).
2. The SPECIFIC thing asked for (a code, a dollar limit, a deadline) vs.
   just meta-information (a handbook name, a "see policy X" pointer with
   no actual value).

Respond with JSON only:
{
  "satisfied": <bool>,
  "uncovered": [<specific missing sub-part, one string each>],
  "suggested_next_query": "<a single search query that would close the
    largest gap, or empty string if satisfied>"
}

If the question only has one part and the answer addresses it with a
real value (not just a pointer), satisfied=true. Don't invent gaps that
weren't asked about.
```

**Call:**
```python
_cc_raw = _call_llm_json(
    _COMPLETION_CRITIC_SYSTEM_PROMPT,
    _build_completion_critic_user_message(question=..., answer=answer),
    ctx=ctx, stage="react_completion_critic", max_tokens=400,
    reasoning_depth="fast", latency_budget_ms=1500,
)
```

`stage="react_completion_critic"` is a **new** model_registry stage,
needs an entry (fast/cheap tier — Flash-class). **Dependency flag:**
`model_registry.py` is currently mid-flight with #103's concurrent
changes in this shared checkout — coordinate the stage addition with
whoever lands #103 rather than adding it blind.

## 5. Loop-back mechanism — context injection, not a forced tool call

The harness doesn't pick tools directly; the model does, informed by
rendered context (`build_reasoning_context`). Consistent with that,
`uncovered`/`suggested_next_query` don't force a specific tool call —
they render as a new context section for the next round, same tier as
#89/#90's prior-turn sections:

```
## Coverage check — this answer was reviewed and found incomplete
Still missing: {uncovered joined}
Suggested next search: {suggested_next_query}
```

The model still decides tool/inputs for the next round itself, now with
this signal available alongside the existing Evidence Ledger and
gaps_open/gaps_closed context. This avoids inventing a second
context-injection mechanism when #89/#90 already established the pattern
for "backend-computed signal → rendered section → model decides."

## 6. Open questions for sign-off

**6.1 — Shared vs. separate extension budget (§3).** Sharing is simpler
and keeps the mode's total round ceiling meaningful, but means a
coverage-gap extension can "spend" budget the groundedness floor might
have needed later in the same turn, or vice versa. Recommend shared;
flagging because it's a real tradeoff.

**6.2 — What "chat.thinking" means as a chat_mode gate.** Today
`react_chat_mode_label()` recognizes `agentic` (10 rounds currently,
`governor.py`'s table). Task #104's message says "6 for chat.thinking" —
a different number than what's live now, implying #103 changes this
table. I'm gating on `react_chat_mode_label(ctx.chat_mode) == "agentic"`
as the current equivalent; if #103 introduces a genuinely new mode value
distinct from "agentic," this gate needs to target that value instead.
Needs confirming once #103 lands.

**6.3 — Critic input scope.** Question+answer only (cheap, ~2 short
strings) vs. question+answer+kept-evidence-chunks (more accurate — the
critic could confirm an entity was *searched* and truly absent from
evidence, not just absent from the answer text, catching a case where
the model had the fact and just failed to include it in prose). The
richer input costs more tokens/latency on every completion attempt in
agentic mode. Recommend starting with question+answer only per the
"lightweight" requirement, revisit if false-satisfied verdicts show up
in practice.

**6.4 — New model_registry stage coordination**, per §4 — needs to land
after/alongside #103, not against it.

## 7. Testing plan (sketch, once design is approved)

- Unit: critic prompt/parse round-trip (malformed JSON → fail-open as
  satisfied=true, same defensive posture as `parse_critic_response`).
- Unit: gate skipped when `rn >= max_it` (no crash, no call).
- Unit: gate skipped outside agentic/chat.thinking mode.
- Unit: extension counter shared correctly with the groundedness floor's
  `_pp_extension_rounds_used` — verify a completion-gate extend and a
  groundedness extend in the same turn correctly sum against one ceiling.
- Integration: multi-entity question (3+ named categories), mock the
  critic to report one uncovered category, confirm round N+1's rendered
  context contains the "Coverage check" section and the loop actually
  continues instead of finalizing.
- Live verify (same pattern as #90/#95): a real multi-part chat.thinking
  question against dev, confirm the critic fires, confirm the extra round
  actually closes the gap it flagged.
