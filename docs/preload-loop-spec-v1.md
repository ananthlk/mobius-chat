# Spec — the preload loop: execute first, let react judge

**Status:** draft for Ananth, 2026-09-12 · **Author:** Governor seat (orchestrator v2)
**Raised by:** Ananth — *"tools_manifest not only selects the tools.. it executes
the top 2-3 tools + rag before react and we reframe it as we already loaded the
first round — tell us if this answers, else tell us what gaps. and based on the
gaps we preload again."*

---

## 1. The problem, measured

Last 1000 turns, per round:

```
ROUND  rows   opened_gap   closed_gap   called_tool
  r1   382    0 (0%)       0 (0%)       333 (87%)
  r2   302    88 (29%)     0 (0%)       151 (50%)
  r3   143    77 (54%)     8 (6%)        35 (24%)
  r4    37    26 (70%)     9 (24%)       14 (38%)
```

**Round 1 opens zero gaps and closes zero gaps, in 382 of 382 rounds.** It fires
a tool blind and its only product is evidence for round 2 to think about. An LLM
call and 10-70s of wall clock to decide which search to run.

Related, same window: **509 gaps opened, 69 closed (14%)**, and **0 of 62
multi-part turns closed every part.**

## 2. The inversion

```
TODAY      react decides  ->  we execute  ->  react judges  ->  react decides ...
PROPOSED   we execute     ->  react judges ("does this answer it? what is missing?")
                          ->  we execute the gaps  ->  react judges ...
```

Round 1 stops being a decision round and becomes a **judgement round over evidence
that already exists**.

## 3. What executes, and who chooses

`toolreg.estimate(query, budget_ms, budget_c, gaps_open, caller_mode, ...)`
already returns a ranked `Offer` — measured at 1.1ms steady state, 12 tools and
1,683 tokens where v1 offers 57 and 14,271.

**Preload = execute the top N of that offer, concurrently, before react speaks.**

| | value | why |
|---|---|---|
| N | **3** (2 ranked + rag) | rag is `tier=default`, always offered, and is the only tool with a latency that matters (p50 3.2s, p90 16.6s, n=274) |
| concurrency | **all N in parallel** | they are independent by construction — no tool's input depends on another's output at this stage |
| cost ceiling | `estimate`'s existing `budget_ms` / `budget_c` | already enforced; a refusal (`irrelevant` / `unaffordable` / `exhausted`) is binding and means "preload fewer", never "retry" |
| wall clock | **max(N), not sum** | this is the concurrency win Retriever's slot fix could not deliver, because the fan-out now happens on OUR side where the calls are genuinely independent |

**Nothing new decides anything.** `estimate()` already ranks; the governor already
holds the gap ledger. Preload is an executor, not a third author.

## 4. What react is asked

Round 1's frame changes from *"choose a tool"* to:

```
[§10 PRIOR RESULTS]   <- now populated on round 1, not empty
  rag                     -> 15 passages, 7 docs
  appeals_get_playbook    -> 3 passages
  service_line_coverage   -> nothing

[§6 ROLE this round] judge what has already been retrieved
[§8]  Does this answer the question?
      - if YES: answer it, cite it, set is_complete
      - if NO:  name EACH part still missing as its own gap. Do not choose a
                tool -- the next preload is chosen from your gaps.
```

**react never picks a tool in the preload loop.** That is a real reduction in its
job and the reason the reframe is safe: it is being asked the thing it is best at
(judging evidence) and relieved of the thing round 1 does blind.

## 5. The loop

```
preload(from question)        -> N tools concurrently
  react judges                -> answer | gaps[]
    if answer  -> done
    if gaps    -> preload(from gaps)   <- estimate(gaps_open=[...]) picks again
      react judges            -> answer | gaps[]
        ...
  bounded by: MAX_PRELOADS (2), the promise, and the hard ceiling
```

**The three-payer question under this loop:**
```
preload 1   rag x3 concurrently (Molina, Sunshine, UHC) -- named by the question
react       "this answers Molina and Sunshine; UHC missing"
preload 2   rag(UHC) + 1 ranked alternative
react       answers all three
```
Two judgement rounds, two preloads, and the per-payer retrieval is `max()` not
`sum()`. Tonight's best run took **4 rounds and 165s against a 95s promise.**

## 6. 🔴 What this risks, stated before anyone builds it

**1. Speculative execution is paid whether or not it was needed.**
80 of 1000 turns complete in one round today. If preload fires 3 tools on a
question that needed one, we pay 3x for a third of traffic. **Mitigation:** N is
budget-derived, not constant — `estimate()` already refuses what a budget cannot
carry, and a single-part question with one high-ranked tool should preload one.
**This must be measured, not assumed.**

**2. It moves cost from a round we might not have taken to a round we always take.**
Today a turn that answers in one round pays one tool call. Under preload it pays
N. The promise is ALREADY breached (thinking p50 103s, p90 241s, 51% over
target), so this cannot be waved through on "it will be faster on average".

**3. react loses the ability to choose a tool it needed and we did not offer.**
Today it can pick anything in the manifest. Under preload it gets what we chose.
**Mitigation:** react may still name a tool in its gap report ("this needs X"),
and the next preload honours it. Without that escape hatch this is a capability
removal, and this session has one of those already.

**4. A wrong preload looks like an absent corpus.**
If we preload the wrong tools and react says "not found", the user gets the same
sentence we spent tonight fixing. **Mitigation:** FRM-7 already distinguishes
never-searched from searched-and-thin; the preload manifest must be recorded per
round so "we never asked the right tool" is a visible verdict, not a shrug.

**5. It is a big change to the one thing that currently works.**
react's method compliance is excellent — reframes 96%, never repeats a query
(0/71), obeys stop instructions 99-100%. **The preload loop changes what react is
FOR.** That is worth doing on evidence and not on a good evening.

## 7. What must be true before it ships

| gate | why |
|---|---|
| **Preload manifest recorded per round** | which tools ran, what each returned, chosen by what. Without it a bad preload is indistinguishable from a thin corpus |
| **Concurrency proven, not assumed** | a test asserting wall clock ≈ max(N), not sum — the exact property Retriever's fix asserts, because it is the whole latency argument |
| **N derived from budget** | a constant 3 is the 57-tool mistake with a smaller number |
| **react can request a tool** | or this is a capability removal |
| **A/B against today** | same questions, both loops. Answer completeness AND delivered latency, because this trades one for the other and we have never measured the first |

## 8. What I would NOT do in v1

- **No preload on `fast` tier.** 13s promise, and its round-1 search compliance
  is already 41% — the tier most likely to pay for tools it does not use.
- **No more than 2 preloads.** The loop must terminate on arithmetic, not on
  react's judgement — that is the 98-round runaway's lesson.
- **No governor-chosen tools.** `estimate()` ranks; the governor supplies gaps
  and budget. Two rankers would be two authors.

## 9. Open questions I cannot answer alone

1. **Tool Manifest:** is `estimate()` intended to become an executor, or should
   preload live in chat and call `estimate()` for the ranking only? My instinct
   is the latter — execution is chat's, ranking is theirs — but it is their seam.
2. **Retriever:** three concurrent rag calls against one request-scoped
   `AsyncSession` is the hazard they already flagged on their own gather. Does
   preload need N sessions, or does the HTTP boundary make this moot?
3. **LLM seat:** round 1's shape changes from "choose a tool" to "judge this".
   That is a new agent_role and a real `response_shape` variant, not a tweak.
4. **Ananth:** 80 turns in 1000 complete in one round today. Is paying N tool
   calls on those acceptable to make the multi-part case work?

## 10. Recommended sequence

1. **Measure the counterfactual first.** For 50 recent multi-part turns, compute
   what `estimate()` WOULD have preloaded from the question alone, and compare it
   to the tools react actually chose across the turn. If preload would have
   picked the same tools, the win is real and mechanical. **If it would have
   picked differently, this spec is a hypothesis and should be labelled one.**
2. Build preload behind a flag, `thinking` tier only, N from budget.
3. A/B on the 20-question bank. Completeness and latency, both reported.
4. Only then consider `normal`.

**Step 1 costs nothing and is the honest gate.** Tonight produced four withdrawn
causal claims; this spec should not become the fifth.
