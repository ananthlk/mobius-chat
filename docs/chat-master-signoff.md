# Chat Master — sign-off, chat refactor program §5

**SIGNED 2026-09-09.** Ratifies three things: (a) the chat-assigned bugs are
correctly ours and correctly described, (b) the P1→P5 phase order in
`scripts/platform/refactor_roadmap.py`, (c) the `master_objective`
revive-or-retire call.

Four record-items attached as **notes, not conditions**. Verified against the
generated `docs/chat-refactor-roadmap.md`, not against the requester's summary.

## (a) Attribution — correct, and incomplete in one direction

Nothing is misattributed *to* chat. Every row owned `chat` is ours and correctly
described.

But **29 of the 71 sequenced bugs carry owner `—` while sitting on chat's own
nodes**: `active_context`, `emit_envelope`, `governor`, `critic`, `classify`,
`plan`, `curator_tools`, `feedback_signal`, `completion_extension_gate`. So
"chat 42" understates; the honest figure is between 42 and 71.

The roadmap header reads `0 unassigned`, which means phase-assigned. 41% of
sequenced bugs have no *owner*. A metric that reads green while the ownership
column is empty is the same shape as the sign-off table that sat at one checked
row through two completed phases.

**Separately: several unowned rows under `emit_envelope` are generalisations,
not defects** — "an import edge is not a consumption edge", "a name-based search
answers 'is there a symbol called X', never 'does X happen'", "a read-back of the
wrong artifact is indistinguishable from a successful one". A lesson filed as a
bug can never be closed. It sits ☐ forever and makes the tracker's completion
metric unreachable by construction. These belong in a principles doc the roadmap
links, not in a table with checkboxes.

## (b) P1→P5 order — right, with a gate defect found by executing it

P1-first held as argued: deleting dead code made the rest legible, and the
stated cost — forfeiting any latency claim — was honest.

**P2's gate is insufficient as written.** It reads "every segment timed;
invariants I1-I7 computable from emitted telemetry alone". That was met, and the
numbers were still wrong three times on 2026-09-09:

- a 30s RAG call read as 30s of **our processing** (external wait, no span);
- 11.5s of integrator **model** time read as `integrate`'s own code (worker-thread
  calls invisible to the trace);
- connection-acquire time read as **query** time (a per-target counter cannot see
  the cost of *reaching* the target).

All three passed "segment timed". **Timed is not attributed**, and a timed
segment that misattributes is worse than an untimed one — it sends someone to
optimise a module that is idle.

Proposed addition: *"and each timed segment's attribution is verified against a
known-external call — an LLM or HTTP boundary crossed inside it must appear as
such, not as processing."*

## (c) `master_objective` retire — right call, incompletely executed by us

The call was right: the writer was dead and four readers returned `"resolved"` on
a default. Reviving a producer to feed readers that had already silently degraded
would have been rebuilding a thing to justify its consumers.

**The retirement left the program's own defect class behind.**
`ThreadState.master_objective` still exists (`app/state/model.py:23`), round-trips
through `from_dict`/`to_dict`, sits in `DEFAULT_STATE`
(`app/storage/threads.py:48`), and **`apply_delta` still accepts it as a settable
key** (`model.py:79`) — while nothing writes a value. A declared, persisted field,
permanently `None`, whose surface still advertises a capability nothing provides.
`orchestrator.py:189` also still names it as an answer source in a debug string.

Recorded against ourselves. Not tidied silently — it should be sequenced.

## (d) The roadmap says the program has not started

Every phase in `docs/chat-refactor-roadmap.md` reads `☐ not started`, and the file
states "Nothing starts until its sign-off table is complete". P1, P2 and the first
P3 node are done. Since the tracker is generated, that is a `gen_roadmap.py` input
problem, not a hand-edit.

## Contract-tag convention (first tag, `2270d72`)

`guards("<node_key>:<guarantee_slug>")`, node_key matching the schema node exactly
— the same binding rule the latency spans use.

**Proposed addition to the convention:** a tag is valid only once the tagged test
has been **demonstrated** to fail with the guarantee removed. On the first use,
one of four tagged tests passed with the guarantee deleted — it snapshotted its
`before` value after the turn ran, so the row was already overwritten and the
assert held via an unrelated compare-and-set miss. Shown, not judged; otherwise
Layer 2 is a machine-checkable marker whose content is "someone thought this was
relevant", believed *because* it carries a tag.
