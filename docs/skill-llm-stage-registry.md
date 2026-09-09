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

**OPEN QUESTION for the payor / deep-research seat**
`PAYOR_STAGES` (mobius-payor/app/llm_manager_client.py) declares `payor_classify`
and `fact_shape`; **neither is in our allowlist**, so a hosted call on either
gets HTTP 400 while dev works. Their coordination rule was followed on their
side — our half was never done.

Deliberately not added: their own comment calls `fact_shape` a prototype, and
allowlisting a stage is a scoping decision about an authenticated internal
endpoint — the stage owner's call, not ours. Awaiting their answer on which of
the two should be live. If `fact_shape` stays dev-only, the 400 is a known state
rather than a surprise at deploy.

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
