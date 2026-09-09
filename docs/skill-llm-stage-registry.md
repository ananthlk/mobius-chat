# `/internal/skill-llm` — stage allowlist ↔ model routing

Two lists must agree for an external seat's LLM call to work properly, and
neither failure is loud.

| Condition | Symptom |
|---|---|
| Stage missing from `_SKILL_LLM_ALLOWED_STAGES` (`app/main.py`) | HTTP **400 Invalid stage** — hosted only; dev falls back to direct Vertex and *works*, so it looks fine until deploy |
| Stage in the allowlist but in **no** model's `eligible_stages` | Call **SUCCEEDS**. `_get_candidates` returns empty, falls through to `fallback_no_models("gemini-2.5-flash")`, and silently bypasses the bandit and its analytics |

The second is the dangerous one: every observable signal says the call worked.
`fallback_no_models` returns a real answer from a real model — the only thing
lost is the *choosing*, and nothing downstream reads which arm was picked. It is
not a gate with no caller; the gate fires. It is a **fallback indistinguishable
from success**.

`tests/test_internal_skill_llm.py` pins both directions:
`test_every_allowlisted_stage_has_an_eligible_model` fails on a new unrouted
stage, and `test_known_unrouted_list_does_not_rot` fails if an exemption is left
behind after the stage is routed — so an exemption cannot outlive its problem.

## Current state (2026-09-09)

**Routed this pass**
- `research_parse` → {flash, pro} — deep-research seat. Prose → descriptive
  fields only; never fills anything that grants permission.
- `payor_fact_reverify` → {flash, pro} — at the stage owner's request. NOT
  pro-locked like `rag_fact_check` / `rag_eval_adjudicate`: those are locked
  because Eval owns their rubric and wants a deterministic ruler. This stage is
  only the "ask the question again" half, so it is a candidate for comparison,
  not a ruler.

**Allowlisted, unrouted — other seats', reported not fixed**
Picking someone's candidate pool blind is how you get a bandit arm nobody chose.
- `appeals_investigation`
- `org_intel_report`
- `org_intel_synthesis`

**RESOLVED 2026-09-09** — the payor seat confirmed both are live and asked for
both. `payor_classify` and `fact_shape` are now allowlisted and routed on
{flash, pro}. Reachability verified here rather than taken on trust:

- `fact_shape` — `fact_loop.execute_run`, spawned by `POST /api/facts/run`
  (mobius-payor/app/routers/facts_admin.py:85).
- `payor_classify` — `source_run.py:382` → `classifier.classify_llm`, the
  low-confidence keyword fallback, behind `use_llm_fallback`. (Their comment
  names `classifier.classify_document`, which does not exist; the stage is real,
  the path is this one.)

`payor_classify` is NOT pro-locked despite being classification against a fixed
rubric. The lock precedent is narrower than "consistency matters": rag_fact_check
and rag_eval_adjudicate are locked because Eval owns their rubric and needs a
deterministic RULER. payor_classify is a fallback that degrades to the keyword
result and never fabricates, running at corpus scale (574 docs measured, 5,257
for AHCA). Locking a bulk pass with no ruler requirement buys determinism nobody
asked for at a cost everybody pays. One line to lock it later if drift appears.

## Also fixed this pass
`SkillLLMRequest` had no `response_schema` field. Callers were sending it,
Pydantic ignored it, and it was silently dropped — structured output worked
against a caller's local dev fallback and would have degraded to free text on
the first deployed call, with no error. `llm_manager.generate` had supported it
all along; only that hop was missing.

Related: `app/services/llm_provider.py:647` `_vertex_schema()` normalises
ordinary JSON-Schema to the Vertex SDK's uppercase type enum. A caller that
talks to the Vertex SDK directly needs its own copy — that is a provider dialect
adapter (a translation whose source of truth is the SDK), not a duplicated rule,
so it is not expected to drift the way a duplicated policy does.

---

# The same trap, three hops (2026-09-09)

One defect shape appeared at every hop of the browser-extension user-fetch lane,
and each instance was invisible in the environment being tested:

1. **extension -> chat.** `/chat/upload` did not declare the Form fields. An
   undeclared Form field in FastAPI is not an error — it is absent. 200 back,
   provenance gone.
2. **chat -> rag.** Fixed by forwarding... in the INBOUND shape. The hops differ:
   the extension sends multipart FORM, rag declares QUERY params. A form-shaped
   forward makes rag return 200 and drop all five — the same silent drop moved
   to the last hop, after both sides believe they are done.
3. **reading the contract.** I checked rag's source and concluded four fields
   were unsupported. The DEPLOYED service accepted all five; my checkout was
   stale (shared checkout, another session had moved it). The extension seat
   found it by PROBING the deployed contract instead of reading it.

**The rule that falls out:** for a cross-service contract, the deployed
service's OpenAPI is the authority, not the file on disk — and the hop's SHAPE
(query vs form vs body) is part of the contract, not an implementation detail.
Both hops returning 200 proves nothing about whether anything arrived.

`access` is forwarded verbatim and NOT validated chat-side. rag maps it through
a Crawler-frozen closed map and 422s on an unknown value rather than defaulting
the classification caller. Copying that map here would duplicate a RULE, and a
duplicated rule drifts — as distinct from a provider dialect adapter, which is a
translation whose source of truth is the SDK. rag owns the answer; chat passes
the question through and lets the 422 surface.
