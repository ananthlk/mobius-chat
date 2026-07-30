# STATUS — LLMManager v2 is LIVE on dev (2026-07-26)

**For:** Chat Master, UX, Product-Awareness, Technical Health, Compliance, Database, Eval, Broadcaster, Chat front end.
**From:** LLMManager.
**One line:** the composable-block prompt engine, its DB persistence layer, the modular enricher, and the v2 tabbed-bubble frontend are all live and verified in production on dev — not staged, not tested-in-isolation, actually running and confirmed via logs + a live screenshot.

---

## 1. What's live, right now

| Layer | State | Proof |
|---|---|---|
| **`prompt_blocks.py` / `BlockAssembler`** | Built, tested (27 unit tests) | authority-last, fail-closed conditional-drop, `validated_at` gate, coherence check, full-sha256 `composition_hash` |
| **Migration 053** (blocks/compositions/members/snapshots) | **Applied to dev DB**, Database-ratified | Both triggers (`tg_authority_immutable_kind`, `tg_block_body_immutable`) functionally verified firing against the live DB, not just DDL review |
| **`PromptManager.resolve_composition`** (async) + `resolve_composition_sync` (sync) | **Live read path** | Round-trips to the same `composition_hash` seeded; PHI-conditional (hipaa_context block only appears when `hipaa_on=True`) |
| **Modular enricher** (`factual` + `blended` compositions seeded) | **Live in production** | Real turn logged `[integrator] v2 composition prompt module=integrator_enricher_blended hash=c0c6f950690db74b` — this is the exact block manifest that built that answer |
| **Chat frontend v2** (tabbed bubble) | **Live, deployed same revision** | Screenshot: Summary/Citations(3)/Corrections(1)/Follow-up(4)/Tasks(3)/Diagnostics tabs, gaps rendered as an inline Summary callout per UX's ruling, a real correction fired and populated its own tab |
| **Control vocab** (Broadcaster mapping + Eval's 3-tier speed model) | **Live, tested (27 tests)** | REFUSE on code-path unknowns, degrade-with-warning on legacy ingestion, quarantine (not coerce) on strategy-axis leaks |

**Serving revision:** `mobius-chat-00592-8r7` · `MOBIUS_PROMPT_SOURCE=composition` · smoke 5/5.

---

## 2. What this replaces

Before today, every enricher call ran a hardcoded, monolithic prompt string in `chat_config.py`. Today, the `factual` and `blended` consolidator paths resolve their system prompt from **versioned, typed, owned blocks in Postgres**, assembled with structural guarantees (authority-last, HIPAA block un-droppable except by explicit fail-open-never policy, `composition_hash` traceable to the exact block manifest for every logged call).

## 3. Verified, not asserted — the discipline behind this status

Every claim above was checked directly before being reported as done, including several times where an earlier "it's live" turned out to be wrong on inspection:
- A "successful" deploy that silently shipped without the feature flag (a hardcoded env-var allowlist in `deploy.sh`, independent of the `.env` file — documented in `RUNBOOK_V2_DEPLOY.md` §0 since it's a fleet-wide deploy trap, not v2-specific).
- A completed chat turn that looked right but had silently fallen back to the old hardcoded prompt (a DB password-injection bug, fail-soft masked it from the user but not from the logs).
- A live-traffic check that returned nothing because of a log-field mistake (`jsonPayload.message`, not `textPayload`).

All three are fixed and now documented as fleet-level gotchas, not just resolved locally.

---

## 4. Remaining work (not gating; already sequenced)

| Item | Status | Depends on |
|---|---|---|
| **Bubble-backend delegation cutover** (`integrate.py:77` + `assistant_envelope.py:417` → Chat front end's parity-proven builders) | Contract 3/3 co-signed; both halves ready | My return to persistence work — atomic, single PR |
| **Transform-output endpoint** (format-transform + egress/PHI gate) | Spec pinned (§3.1), not built | UX/PA surface list finalized |
| **AC-v2-11** (graded promise-KEPT badge: HIPAA-compliance + groundedness) | Contract pinned with Eval | Part-2 (groundedness) buildable now; Part-1 (authoritative-source-cited) waits on Retriever's authority-fix |
| **True-progressive §2.1** (ReAct streams as phase-1) | Deferred by Chat Master ruling | Off-ReAct migration |
| **Composition Studio UI** | Prototype built, persistence live | Wiring the prototype to the live tables |
| **Envelope-format compliance** (model inconsistently defaults to bullets over table/stats/bars even when content qualifies) | Diagnosed, a stronger instruction tested well in one trial | Not shipped — held per product owner's call; revisit if it recurs as a real problem |

## 5. One live product-quality note (flagged, not shipped)

Structured-envelope selection (table/stats/bars vs bullets) is **inconsistent** — the model sometimes correctly picks a table for comparable rows and sometimes defaults to bullets for the same kind of content, even though the instruction is present and correctly delivered in every composed prompt. A stricter, example-driven rewrite of that one rule showed a positive signal in a single A/B trial (Haiku, real API). Not shipped — this is a live-editable block (no redeploy needed to fix later), and the product owner asked to hold rather than act on a one-trial signal. Worth Eval or Tech Health's eye if inconsistent formatting becomes a recurring complaint.

---

*Full technical detail: `SPEC_LLMMANAGER_V2.md` (control plane + gate history), `SPEC_PROMPT_BLOCK_PERSISTENCE.md` (schema + Database's ratification), `RUNBOOK_V2_DEPLOY.md` (deploy record + the 4 bugs), `docs/bubble-backend-contract.md` (BFF seam, 3/3 signed).*
