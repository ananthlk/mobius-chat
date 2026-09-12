# The round frame — ten sections, ten acknowledgements

**Status:** proposal, 2026-09-12 · **Author:** Governor seat
**Raised by:** Ananth — *"we introduce 1) mobius 2) user 3) question 4) what we
have so far 5) open gaps 6) role we want them play 7) output that is specific
to role 8) opinion on if this is complete 9) tool set they have access to
10) prior tool results.. i want an acknowledgement against each step."*

---

## Why the acknowledgement is the point

Sixteen instructions reach react on round 1 today. **Six of them cannot be
observed at all**, and they cluster in one shape — *"aim for higher precision",
"resolve avoidable ambiguity", "are you satisfied"*. Every one is a disposition
with no act attached, so the trace cannot show whether it landed.

An acknowledgement attaches an act to each section. It converts *"did it read
this?"* from unanswerable into checkable.

### What an ack DOES and DOES NOT prove — the distinction must hold

**An ack proves RECEIPT and COMPREHENSION. It does not prove COMPLIANCE.**

*"I am working the Sunshine gap"* and then querying Molina is a **contradiction
we can see** — which is worth a great deal — but the ack itself is still a
self-report, and this program has already shipped a model asserting "no sources
found" beside a confident answer.

So every ack is cross-checked against something observed, and the two are
recorded separately:

```
ack_says          what react claimed          (self-report)
observed          what the round actually did (trace)
agreement         true | false | unobservable (the useful signal)
```

**Disagreement is the finding, not the ack.** An ack that always agrees with
itself measures nothing; an ack that contradicts the round tells us precisely
where the instruction is being read and then not acted on — which is the thing
Ananth asked for and the thing we cannot get any other way.

---

## The frame

Ordered by EXECUTION order (`docs/governor-profile-statements.md` O2), because
the model reads top-to-bottom and acts in sequence.

| § | section | what it carries | who owns it |
|---|---|---|---|
| 1 | **MOBIUS** | what this system is and is for | LLM seat (system block) |
| 2 | **USER** | who is asking; constraints that bind (brevity, org, role) | Chat |
| 3 | **QUESTION** | the question verbatim | Chat |
| 4 | **SO FAR** | running answer; what is established and cited | react's own, carried |
| 5 | **OPEN GAPS** | each gap: text, attempts, what each returned, closure | **Governor** |
| 6 | **ROLE** | the posture's role this round | **Governor** |
| 7 | **OUTPUT** | the response shape this role must produce | LLM seat |
| 8 | **COMPLETE?** | the satisfaction question; the governor's dissent | **Governor** |
| 9 | **TOOLS** | the tools offered THIS round, and why | Tool Manifest |
| 10 | **PRIOR RESULTS** | the last tool's returns, addressable by [N] | react's own |

§5, §6 and §8 are the governor's, and they are exactly the three things react
cannot hold for itself: the cross-round ledger, the role it is being asked to
play, and a second opinion on whether it is done.

---

## The acknowledgements

One per section. **Each must be checkable against something we already have** —
an ack that can only be compared to itself is a longer way of saying nothing.

```jsonc
"ack": {
  "scope":        "payer-policy question, answerable from corpus",   // §1
  "user":         "no brevity constraint",                            // §2
  "parts":        ["Molina ...", "Sunshine ...", "UnitedHealthcare ..."], // §3
  "established":  ["Molina ICM program, cited [1][2]"],               // §4
  "working_gap":  "S384280",                                          // §5
  "role":         "explore",                                          // §6
  "complete":     false,                                              // §8
  "complete_why": "two parts never searched",                         // §8
  "dissent":      "accepted — searching UnitedHealthcare this round", // §8
  "tool":         "rag",                                              // §9
  "kept":         [6, 9, 12]                                          // §10
}
```

### What each ack is checked against

| ack | cross-checked against | catches |
|---|---|---|
| `scope` | — | weak; drop unless it earns its place |
| `user` | the profile we sent | a brevity constraint read and then ignored |
| `parts` | `_targeting(parts, query)` | **the 3/3 failure: parts named, query narrowed** |
| `established` | `cited_source_indices` | a claim of established fact with no citation |
| `working_gap` | the gap the governor named + `_targeting` on the query | **says Sunshine, queries Molina** |
| `role` | the posture the governor sent | steering read but not adopted |
| `complete` | `is_complete` in the same response | internal contradiction |
| `dissent` | did the next round search the named gap | accepted-in-words, declined-in-fact |
| `tool` | the offered set from `estimate()` | a tool used that was not offered |
| `kept` | chunk ids in the prior result | fabricated chunk references |

**Seven of eleven are checkable today.** `parts`, `working_gap` and `dissent`
are the three that would have caught tonight's failures, and none of them can
be checked without the ack — which is the argument for building this.

---

## What this costs, stated before anyone is surprised

- **Tokens.** Eleven ack fields per round, every round. Small next to a 75,056
  char prompt, but it is not free and it compounds per round. This lands in
  the phase Ananth put LAST (*"tokens and length of answers"*), so the right
  move is to build it, measure the per-round delta, and then cut acks that
  never disagree — **an ack that has never once contradicted the trace is
  measuring nothing and should be deleted.**
- **A new way to be wrong.** A model that learns to emit a plausible ack
  without acting on it produces a *worse* signal than no ack, because it reads
  as evidence. The cross-check is what stops that, so **no ack ships without
  its checker.** An ack whose checker is "not built yet" is a producer with no
  consumer.

## Sequencing

1. §5, §6, §8 — the governor's three sections, from the statement registry that
   already exists.
2. `parts`, `working_gap`, `dissent` acks + their three checkers, together.
   These three earn the whole change.
3. The rest of the frame with the LLM seat (§1, §7) and Tool Manifest (§9).
4. Measure disagreement rate per ack. Delete the ones that never disagree.
