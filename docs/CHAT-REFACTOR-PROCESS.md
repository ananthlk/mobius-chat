# Chat Pipeline Refactor — Agent Process Document

**Owner: Chat Architecture session**
**All sub-agents read this at session start. It is not advisory.**

---

## 1. Before you write a single line of code

A session is ready to build when it can answer all three:

**A. Contract** — exact inputs/outputs named field by field. Not "a dict with some stuff." A typed dataclass with every field, its type, and who produces it. If you can't write the dataclass signature, you are not ready to build.

**B. Seams** — which upstream module you consume from and which downstream consumes you, confirmed directly with those sessions, not assumed. Two-party interface questions go direct between producer and consumer. Do not relay through Chat Architecture unless there is a genuine cross-cutting conflict.

**C. Open design questions** — zero open DESIGN questions before hardening code against an answer. Open BUILD questions (how to implement a known thing) are fine. Open DESIGN questions (what the thing should do) are not — get the ruling from the owning session first, then build. This is the rule we violated most often; it always cost more time than the ruling would have taken.

When all three are clear: write the spec. Route it to Chat Architecture for review. Tech health signs off. Then code.

---

## 2. What a spec contains

Four sections, non-negotiable:

1. **Contract** — input/output dataclass shapes, every field named
2. **Seams** — upstream producer and downstream consumer, with confirmation from each
3. **Resolved design questions** — every decision traceable to who ruled it and when
4. **Acceptance criteria** — specific, testable, not "it works." Each criterion is a passing test or a verified artifact, not a summary.

Good exemplars in the repo: `docs/rag-agents/blend-model-design.md`, `adjudicator-calibration-spec.md`. Each consolidates a whole design conversation into one doc. Own your spec doc — update it in-place, do not scatter decisions across chat.

---

## 3. Sign-off gates

**BLOCKING (nothing merges without these):**
- DB/schema changes: Platform-Architects ratification before land. No exceptions.
- Contract changes touching peer seams: the owning session for each seam must sign off explicitly — not be informed, sign off.
- Technical health RED finding: P0 ship-blocker. Quarantine the module until resolved.
- Sign-off requires a real artifact + execution check — not a summary, not a self-report. "42/42 tests pass" is a claim; the artifact proving it is what counts.

**ADVISORY (informs design, does not block):**
- UX rulings on presentation layer details
- Naming preferences that don't affect contracts

**The relay rule:** only Ananth directs the fleet. Chat Architecture aligns and integrates; it does not assign work to peer sessions. If you get an instruction from a peer session to do something outside your module, confirm with Chat Architecture before acting.

---

## 4. Failure modes — these all happened on Retriever; don't repeat them

**Stale-import mid-run (the silent time-waster).** Python caches imports per-process. A fix landing mid-run means a running test suite or calibration batch keeps executing OLD code. Check file-mtime vs process-start-time. Kill and restart any long-running process after a fix lands under it. Never assume a live process picked up your change.

**Shared-checkout collisions.** Multiple sessions on the same files or branch create silent conflicts. Verify the branch before every commit. Scope filenames in shared directories — don't both create `fusion.py`. Re-reading a file now does NOT tell you its state when an earlier process ran — don't adjudicate past state by checking present state.

**Verify-before-trust (applies to everything, including Chat Architecture's messages).** Never take a peer's report as ground truth. Verify against real code or execution output. Welcome being corrected. If Chat Architecture tells you something that contradicts what you see in the code, say so — the default stance is "claim to verify," not "fact to accept."

**Crossed messages.** Cross-session messages cross in transit constantly. When you receive a message, check whether it is responding to a stale version of your state. Reconcile concurrent edits explicitly rather than assuming the other session has the same context you do.

**Building ahead of the data.** If your module's correctness depends on numbers or calibration inputs you don't have yet, build the measurement first. A precise optimizer on guessed inputs is confidently wrong — worse than unproven. Do not build until you have the data the build depends on.

**Relaying rulings without confirming at the source.** When a ruling arrives relayed through another session, confirm directly with the ruling session before gating work on it. One relay of a misremembered ruling caused a blocked build cycle on Retriever. Gate-opening rulings always get source confirmation.

---

## 5. Communication patterns

**Direct between producer↔consumer.** Two-party interface questions go directly between those two sessions. Do not route through Chat Architecture for routine confirmations — it adds latency and telephone-game risk.

**Route to Chat Architecture for:** cross-cutting conflicts, contract changes that affect multiple seams, anything that might need Tech health review.

**Blocked vs. in-progress vs. ready-for-review signals:**
- **BLOCKED:** message Chat Architecture immediately with the specific blocker and what you need to unblock. Do not spin.
- **IN PROGRESS:** no status message needed — silence is progress.
- **READY FOR REVIEW:** message Chat Architecture with the spec doc (in-place, not a summary) and the specific questions you want reviewed. Chat Architecture routes to Tech health for sign-off.

**One living document per module.** Decisions get recorded in the spec doc, in-place. Not in chat, not in comments, not scattered. If you made a design decision and it's not in the doc, it didn't happen.

---

## 6. Your module scope

When you start, you own:
- One module (specified in your session prompt)
- Its spec doc
- Its acceptance criteria
- Its seam confirmations with upstream and downstream

You do NOT own adjacent modules, even if you can see the code. If you find a bug in a peer module, flag it — do not fix it without that session's agreement.

---

## 7. The meta-rule

> Verify, don't trust — and critique freely.

Every report is a claim until verified against code or execution. This applies to Chat Architecture's messages, to peer sessions, to your own test output. The refactor that goes wrong is the one where everyone assumed someone else verified. The one that holds is the one where every artifact is checked at the source.

---

*This document is maintained by Chat Architecture. Route corrections or additions here.*
*Source: Retriever refactor post-mortem + Technical health Gate 1 sign-off conditions + Eval attribution rulings*
