# P3 work order — `tool_manifest`

**Owner** Chat Master · **Ratifier** Tech Review · **Coordinator** Platform seat (mobius-f2)
**Opened** 2026-09-09 · **Follows** the `state_load` pass, same shape

> Ananth, 2026-09-09: *"before we get to this big a refactor we just fix what it is and get it
> to be green and then add these as enhancements.. so get current to work then add these features"*

**This work order covers Stage 1 only — fix what exists.** The three enhancements Ananth named
(manifest-in-persistence, access provisioning, dynamic tool loading) are recorded in §5 so they
are not lost, and are explicitly **out of scope for this pass**. Do not build them here. Do not
design for them here either, beyond not actively blocking them.

---

## 1. The bug, as reported

Three findings on the roadmap, and the working hypothesis is that they are **one defect**:

| Finding | What was observed |
|---|---|
| TOOL SELECTION IS SPORADIC, AND THE MODEL NARRATES A MISS AS A BROKEN TOOL | the same question gets a tool call on one turn and not the next |
| A TOOL RETURNED A REAL PLAYBOOK AND CHAT REPORTED THERE WAS NONE | the tool succeeded; the answer said it found nothing |
| SPORADIC TOOL SELECTION — FOLDED INTO THE TOOLS REFACTOR | three candidate layers named, none eliminated |

Ananth's read, verbatim: *"i think the retries or json formatting or something is wrong.. not
clear if it is in react not getting right, tool manifest or executor"*.

**That uncertainty is the point of Stage 0.** We do not know which layer it is. Three layers can
produce the identical user-visible symptom:

- **L1 — the manifest doesn't offer it.** `_resolve_allowed_tools` (app/pipeline/orchestrator.py:95)
  narrows by mode default, then by `user_tool_subscriptions` (app/storage/tool_policy.py). If the
  tool is not in `ctx.allowed_tools`, the model cannot call it and will say so in prose.
- **L2 — react doesn't ask.** The manifest is offered, the model doesn't emit a call, or emits one
  that fails to parse. This is where "json formatting" would live.
- **L3 — the executor returns and nothing reads it.** The tool ran, returned a real result, and the
  result did not reach the answer — the playbook case.

**L1 and L3 are indistinguishable from L2 in the answer text**, because in all three the model
narrates an absence. That is why this cannot be diagnosed by reading the transcript.

---

## 2. Stage 0 — DIAGNOSE BEFORE FIXING (do this first, report before touching code)

This is the step the `state_load` pass did not need and this one does. **Do not start fixing
until Stage 0 names the layer**, and do not fix all three layers defensively — a speculative fix
in the wrong layer is a change we cannot attribute.

The instrument already exists: P2b spans are bound to schema node keys and there are three
tool-selection counts live. Use them.

**Produce a per-layer count over a real window** (state the window and the row count; a count is
signal at n=1 per Eval's ruling, but say what n is):

1. `tools_offered` — how many turns had a non-empty `ctx.allowed_tools`, and the size
2. `tools_called` — how many of those turns emitted a parseable tool call
3. `tools_returned_nonempty` — how many calls returned a real result
4. `answer_claimed_absence` — turns whose answer asserts nothing was found

The drop between any two adjacent numbers **is** the layer. Report it as a funnel, with the
absolute counts, not percentages alone.

**Then reproduce ONE instance end to end.** Take a single turn where a tool returned a real
playbook and the answer said there was none, and follow it through all three layers with the
persisted evidence. If the evidence to do that is not persisted — say so; that is itself the
finding and it is the same producer-without-a-consumer class, and it changes this work order.

**Report Stage 0 to me before writing a fix.** I will not treat a layer as identified on a
plausible reading; per the standing rule, a name-based search answers "is there a symbol called
X", never "does X happen".

---

## 3. Stage 1 — fix, to green

Scope is **the layer Stage 0 names**, plus anything Stage 0 proves is also broken. Not the
whole subsystem. `react_loop.py` is 6,200 lines; this pass is not licensed to restructure it.

Two findings must be closed regardless of layer, because both are correctness-visible:

- **The model must not narrate a miss as a broken tool.** Whatever the layer, a turn where no
  tool was offered and a turn where a tool failed must not produce the same sentence to the user.
  Distinguishing them may require a real signal to reach the answer path — if so, that signal has
  to be *persisted*, not just passed, or we've built another producer with no consumer.
- **A successful tool result must reach the answer.** The playbook case is a correctness bug, not
  a phrasing bug.

**Related item, decide explicitly rather than by omission:** `make_tool_failed` has **zero
callers**, which is why `tool_failed` is structurally impossible and the health signal is green
by construction. It is the origin instance of the systemic class. If Stage 0 lands in L3, wiring
it is probably part of the fix; if it lands elsewhere, say so and leave it — but do not leave it
unmentioned.

---

## 4. Definition of done

Ananth's standing bar — *"test but also make the module production ready — test = unit test +
latency test"* — plus the tag convention that now exists:

- [ ] **Stage 0 report delivered and the layer named**, with the funnel counts and one traced instance
- [ ] Unit tests covering the fixed layer, passing
- [ ] **A contract tag**, e.g. `@pytest.mark.guards("tool_manifest:<guarantee_slug>")` — node_key
      matches the schema node exactly, and **the tag is valid only if the tagged test has been
      demonstrated to FAIL with the guarantee mutated out**. That rule is yours (Chat Master,
      2026-09-09) and it is now binding on every tag; it caught a false tag on its first use.
- [ ] Latency: **counts yes, wall-time only with a ≥5-run noise floor, p50/p95 never mean**, and
      every count carries its target (Eval's ruling)
- [ ] Full suite against `docs/chat-test-baseline.json` — may shrink, never grow
- [ ] Findings updated in `scripts/platform/chat_node_content.py` so the schema, roadmap and
      coverage artifacts regenerate; I run `refresh.sh`
- [ ] Deployed to dev and verified **by image digest**, then Eval audits the tag

---

## 5. SEQUENCED AS P6 — not out of scope

**Corrected 2026-09-09 on Ananth's instruction.** My first draft parked these as
"enhancements". He pushed back: *"not out of scope but sequenced."* He is right — they
are real work with a real gate, and calling something an enhancement is how it quietly
stops being tracked. They are now **phase P6 — Tool selection**, in
`scripts/platform/refactor_roadmap.py`, blocked on this pass closing.

**(a) Manifest in persistence, with a UX** — change a tool without a deploy. Today the
catalogue is code (`app/pipeline/tool_manifest.py`, 694 lines). **Half the substrate
already exists:** `user_tool_subscriptions` (migration 035) persists per-user policy and
`get_allowed_tools_for_user` already reads it at turn start. The gap is the *catalogue*
being in code, not the *policy* — so this is smaller than it looks. Shares P5's control
plane; does not grow a second one.

**(b) Access provisioning** — *who* may use a tool. Adjacent to (a) and a different
question: (a) is what exists, (b) is who may use it. The per-user table is a
**subscription** model, not an **authorization** model. Conflating them is the mistake
worth avoiding while it is still cheap.

**(c) Retrieve the tools for a turn instead of offering all of them.** Ananth's
mechanism: *"a simple even vector search for tool will be helpful or some kind of search
— this will cut short on tokens and make a real good determination and make the latency
also faster."*

Three effects, and they must be **measured separately** because they are different claims:

| Effect | How it is measured | Available today? |
|---|---|---|
| **Fewer prompt tokens** | the manifest is prompt text on every turn — count it | **yes, count it now** |
| **Better selection** | P3's Stage 0 funnel is the before-measurement | after Stage 0 |
| **Lower latency** | a *consequence* of the first two | **do not claim independently** |

That third row is the one to hold the line on. Latency here is downstream of token count
and selection quality; reporting it as its own win double-counts the same improvement.

### The shape that makes (c) work: two representations, not one

**Ananth, 2026-09-09:** *"it allows a tool to fully represent itself, for selection, and
a narrow set of short react briefs travel with it."*

This is the design, and it is what makes the token claim and the accuracy claim
compatible rather than in tension. **A tool gets two representations, used at two
different moments:**

| | **Selection representation** | **ReAct brief** |
|---|---|---|
| Read by | the retriever | the model, in the prompt |
| Cost | matched against, **never spent as prompt tokens** | every token is paid, every turn it survives |
| So it can be | **long and complete** — full description, when to use and when not, worked examples, failure modes, synonyms, the phrasings real users actually type | **short** — name, one line of what it does, the call shape |
| Budget | effectively free; make it as rich as it needs to be | scarce; ruthless |

Today there is **one** representation and it does both jobs, which is why it is bad at
both: every word that helps the model choose correctly is a word paid for on every
single turn, so the description gets trimmed for cost and the selection gets worse. The
manifest is 694 lines of that compromise.

Splitting them removes the tension outright. **A tool can finally represent itself fully
— for selection — because that representation is no longer prompt text.** Only the
narrow brief travels. That is why this cuts tokens *and* improves determination at the
same time; those are not two independent wins to be double-counted, they are one
structural change with two visible effects.

**The second-order effect, which is probably the real prize.** Today the cost of a tool
is paid by *every* turn, including the turns that will never use it — the manifest is
prompt text on all of them. So each new tool taxes the whole system, and the rational
response is to have **few, broad, general-purpose tools**. That is a design constraint
imposed by the prompt budget, not by the problem.

Retrieval removes it. Once a tool costs approximately nothing on turns that do not
retrieve it, the economics invert: **many narrow, specific, well-described tools beat a
few general ones.** A tool that serves 3% of turns is currently not worth its prompt
weight; with retrieval it is straightforwardly worth building. Ananth's read —
*"this will allow for better options"* — is that, and it is the durable part: the token
saving is a one-time win, but the change in what is *worth building* compounds.

It also makes the tool catalogue a place where product knowledge can accumulate. A
narrow tool with a rich selection representation is a specific capability that
announces exactly when it applies — that is a thing you can keep adding to. Under the
current shape, adding is a cost.

**Caveat, so this is not read as a licence to proliferate:** more tools also means more
ways for retrieval to be wrong, and a wrong retrieval is now *invisible to the model* —
it cannot call what it was never offered. That is exactly why the inspector and the
persisted per-turn decision above are gate items and not nice-to-haves. The economics
only improve if the selection stays honest and checkable.

Two consequences worth stating before anyone builds it:

- **The two representations must be authored together and stay consistent.** A rich
  selection text that promises behaviour the brief does not describe gets a tool
  retrieved and then not called — which lands us right back in §2's funnel with a new
  cause. The UX in (a) edits both, side by side, or it is not the right UX.
- **Retrieval quality is now testable on its own**, separately from the model: given a
  query, does the right tool come back? That is a fixture-and-assert question with no
  LLM in the loop, and it is the cheapest test in this whole program. Build it.

### (c) is inspectable in the UX — it is not a separate feature

**Ananth, 2026-09-09:** *"that should be part of the ux build — given a situation what
tools are selected."*

So the manifest UX in (a) is not only an editor. It must answer, for a **given
situation**: *which tools does this turn get, and why?* Type or paste a query, see the
retrieved set, the scores, and what fell below the cut.

This is a correctness requirement, not a nicety, and it is the same argument this whole
program keeps making. A retrieval step that silently narrows the tool list is **a
producer whose decision nothing records**: when the model then says it cannot do
something, nobody can tell whether the tool was withheld or the model failed to call it.
That is precisely the ambiguity §2 exists to resolve — and shipping (c) without the
inspector would **reintroduce it one layer earlier**, after we had just paid to remove it.

Which means the per-turn retrieval decision must be **persisted**, not just rendered:
the UX previews it for a hypothetical query, and the turn record answers it for a real
one. A preview that reads live code while the log keeps nothing is a read-back of the
wrong artifact.

Minimum for the phase gate:
- given a query, show the tools retrieved, with scores, and the ones just below the cut
- show what changed when the catalogue is edited — before and after, on the same query
- for a **real past turn**, show the tools that were actually offered and why

**Why P6 follows P3 and cannot lead it.** Retrieval changes *which* tools are offered.
Ship it while selection is still sporadic and a miss becomes unattributable — retrieval
did not surface the tool, or the tool was surfaced and not called, and we are straight
back to the ambiguity this pass exists to resolve. **P3's Stage 0 funnel is also (c)'s
baseline and its training signal.** That is the strongest single reason to do Stage 0
properly rather than minimally: it is not overhead for this bug, it is the measurement
the next phase is built on.

**Embedding note:** pgvector is the standard — do not introduce a second vector store.
A tool catalogue is small enough that exact search over the whole set is likely viable;
measure before reaching for an index.
