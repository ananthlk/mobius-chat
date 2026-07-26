# Contract — `bubble-backend` (BFF for the chat bubble surface)

**Status:** DRAFT — build against this; **formal co-sign required before merge.**
**Co-signers:** Chat front end (owner) · LLM Agent (enricher-seam) · Chat Architecture (coordinator)
**Seam contract:** `SPEC_CHAT_FRONTEND_V2_UX.md §2` (already signed — this doc does not change it; it records `bubble-backend`'s internal invariants).

---

## What this is

`bubble-backend` is the **single-point, read-only aggregator** for the chat-bubble UX surface — the Backend-for-Frontend (BFF) file the bubble's frontend (`render/bubble` → `answer-card` + `card-render-model`) maps to. It assembles everything the bubble needs to render and hands it over in the bubble's shape. The frontend reads **only** `bubble-backend`; it never reaches into the router/enricher.

This is a **structural consolidation, not a contract change** — it gathers presentation logic that is scattered today into one place.

## The produce / shape cut

| | Produces (the "brain") | Shapes (this BFF) |
|---|---|---|
| Owner | LLM Agent / Chat Architecture | Chat front end |
| Code | `final_parallel.py` — merged A/B/C card + phase signals (the emit) | `integrate.py:42/77` (`_ANSWER_CARD_ENVELOPE_KEYS` allowlist copy) + `assistant_envelope.py:417` (typed-block build from B/C) |
| Rule | The emit contract. **Stays put.** | **Moves into `bubble-backend`.** |

`bubble-backend` **reads** the produced outputs (`final_parallel` merged card, `assistant_envelope` typed blocks, retriever sources) and shapes them for the bubble. It never produces.

## Invariants (all six)

1. **Read-only aggregator.** Reads `final_parallel` merged card + `assistant_envelope` blocks + retriever sources. Nothing else.
2. **Presentation-only.** No LLM calls, no retrieval, no re-ranking, **no mutation of answer content.** If a surface must *change* the answer, that is the emit's job — not the BFF. Enforced structurally (§ below).
3. **Positive-filter allowlist preserved.** The move of `integrate.py:77`'s allowlist keeps it a **positive `allowlist → client` filter** — only allowlisted keys reach the FE. It must **not** degrade into "pass through whatever is on the card," or consolidation silently widens what leaks to the frontend (raw reasoning, `source_confidence_override` internals, etc.).
4. **Semantics vs presentation split.** LLM Agent owns what B/C fields *mean* (`next_steps`, `gaps`, `takeaways`, …). `bubble-backend` owns **tab mapping + envelope selection + phase→slot resolution** (already modeled in `card-render-model.ts`).
5. **No security decision in a presentation layer.** (Forward, for the `surfaces-backend` pair: the egress/PHI gate stays server-authoritative in LLM Agent's transform-output endpoint per §3.1; the BFF renders `egress.channels` hints and calls the export endpoint — it never reimplements the gate.)
6. **Output-shape amendment.** Any change to `bubble-backend`'s **output shape** (what the FE receives) requires co-sign from **both Chat front end AND LLM Agent** — output changes implicate either input semantics (LLM Agent) or presentation logic (Chat front end). Neither changes it unilaterally.

## Single source of truth — MOVE, not copy (the mechanic that makes consolidation real)

The produce/shape cut is a **two-sided, atomic edit**. When `bubble-backend` takes over the shape step, the old sites must **stop** doing it:

- `bubble-backend` **adds** the shape logic, **and** `integrate.py:77` (allowlist copy) + `assistant_envelope.py:417` (typed-block build) are **cut over to delegate** to `bubble-backend` — **in the same change.** No parallel execution.
- If both run, they drift → two sources of truth, i.e. the exact coupling the BFF removes. "Consolidate" must mean **move**, never copy.
- **Coordination:** those two call sites are in LLM Agent's area. The bubble-pair change is therefore two-sided — Chat front end builds `bubble-backend`; **LLM Agent makes the delegation edit on their side, atomic with the build** (flagged on the PR). Neither half merges without the other.

## Structural enforcement (not policy — tests that fail the build)

- **Import-guard test:** `bubble-backend` importing any router / planner / enricher / LLM-client / retriever module **fails the build** (a RED). Enforces invariants 1 + 2.
- **Allowlist-drop test:** a non-allowlisted internal field (e.g. a raw-reasoning key, `source_confidence_override` internals) placed on the input card is **absent** from `bubble-backend`'s output. Enforces invariant 3 — consolidation cannot silently widen the FE leak surface.

## Frontend side of the pair

`render/bubble` — `renderAnswerCard` extracted from `app.ts`, reading only `bubble-backend`'s output. FE-model layer already built + tested: `answer-card.ts` (parse/validate/visibility) + `card-render-model.ts` (field→tab map + additive-merge), 42 vitest tests green.

## Template

This pair is the **template** for every other surface (`tasks`, `diagnostics`, `surfaces`, `vault`, `profile`, …), modularized **incrementally as each is touched** — never speculatively.

---

*Bubble-first BFF pairing. Ratified seam (Chat Architecture) + co-signed invariants (LLM Agent). Build → bring this doc for formal co-sign → merge.*
