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

## 5. OUT OF SCOPE — the enhancements, recorded so they are not lost

Ananth named three, and the sequencing instruction is explicit: get current to work first.

**(a) A UX for the tool and capabilities manifest, written to persistence, so a change does not
require a deploy.** Today the manifest is code (`app/pipeline/tool_manifest.py`, 694 lines).
Note for whoever builds this: **half the substrate already exists** — `user_tool_subscriptions`
(migration 035) already persists per-user opt-in/opt-out and `get_allowed_tools_for_user` already
reads it at turn start. The gap is the *catalogue* being in code, not the *policy*. This is the
same shape as the governor's config UX (P5) and should probably share its control plane rather
than grow a second one.

**(b) Access provisioning** — who is allowed which tool. Related to (a) but a different
question: (a) is "what exists", (b) is "who may use it". The per-user table is a subscription
model, not an authorization model; those are not the same thing and conflating them would be a
mistake worth avoiding early.

**(c) Dynamic tool loading over time** — not every task needs every tool; predict which queries
are likely to need which (Ananth's example: a RAG-type search). This one is genuinely different
in kind from (a) and (b) — it is a *prediction* problem, and it needs the Stage 0 funnel data as
its training signal. Which is a reason to do Stage 0 well: **the diagnosis instrument for the
current bug is the data source for the future feature.**

These are enhancements, and they get their own gate when they open. They are **not** conditions
on this pass going green.
