# Model roster — three findings, deferred by ruling, not resolved

**Status: all three verified live 2026-09-09/10. Ananth ruled DEFER on two;
the third is unruled. None are fixed.** Recorded so they are not re-derived.

> Ananth, 2026-09-10: *"Don't bother about anthropic and groq yet, we will have
> them back just not today."*

---

## 1 · Enabling is per PROVIDER, not per model — the mechanism behind the other two

`model_registry.py` startup:

```python
elif spec.provider == "groq" and groq_key:
    spec.enabled = True
elif spec.provider == "anthropic" and anthropic_key:
    spec.enabled = True
```

**A model is enabled because its provider has an API key, regardless of whether
that model still exists or can be paid for.** `_get_candidates` then admits
anything with `enabled=True` (it only skips `enabled=False`), so the bandit
draws it, the call fails, and the breaker trips — every cycle.

This is the part worth remembering, because it is why the roster **cannot
express an intent about an individual model**. Both findings below are
consequences of it, not separate bugs.

**Verification trap:** a local check shows `enabled=False` for all of these,
because a dev machine has no provider keys. That is an artifact of the local
environment, not the deployed state. `GROQ_API_KEY` and `ANTHROPIC_API_KEY` are
both SET on dev — confirmed against the running service, not inferred.

## 2 · Three decommissioned Groq entries are enabled and drawn

`llama-3.3-70b-versatile`, `llama-3.1-8b-instant`,
`meta-llama/llama-4-scout-17b-16e-instruct` return *"does not exist or you do
not have access to it"*. They carry ~10 eligible stages each, so they occupy a
candidate slot and a Thompson draw on every stage they list, forever.

Not a cost decision — a stale roster. **Deferred by ruling.**

## 3 · The cost posture is enforced by an empty balance, not by configuration

> Ananth: *"stick with gemini for now, anthropic is expensive, we will switch
> when we have everything — that is why we have a llm_manager and modes and
> bandit."*

The decision is deliberate and the mechanism is correct — provider choice being
a runtime decision is what LLMManager is for. But **the roster does not say so.**
`ANTHROPIC_API_KEY` is set, so every Anthropic model is enabled, drawn, and
spends a real call to discover `credit balance is too low`.

So a deliberate decision is currently implemented by an accident, at the cost of
a failed call and a breaker trip per draw. If the intent is Gemini-first,
disabling those entries would express it directly. **Deferred by ruling.**

Corollary worth keeping: **a circuit breaker cannot distinguish a chosen
configuration from a degradation** — the mechanism is identical either way. The
deep-research seat reported this as an incident, correctly on the evidence they
had, and withdrew it when the fleet view arrived. Anyone reading breaker logs
for these providers is reading an intended state.

---

## Unruled: `is_fallback` is structurally always false

Not part of Ananth's ruling; recorded here because it was found alongside.

`llm_calls.is_fallback` and `.fallback_from` exist. **Nothing ever writes them.**
`llm_analytics.py:85` defaults `is_fallback=False` and `llm_manager` never passes
it — `f` on all 1,988 calls in 24h. Meanwhile `orchestrator.py:174` **reads** it
(`if u.get("is_fallback")`): a consumer wired to a signal that cannot fire, the
same shape as `make_tool_failed` leaving `tool_failed` structurally impossible.

Consequence: **a caller cannot tell a routed model from a fallback model, and
neither can the database.** `fallback_no_models` pins to `gemini-2.5-flash`, so a
flash row is ambiguous between "the bandit chose flash" and "there were no
candidates". The only positive disambiguation available today is the presence of
a row for a *different* model on the same stage.

This is a half-built feature that reads as complete — worse than a missing one,
because the column's presence is what stops the next person building it. Fix is
one write at the `fallback_no_models` site. Awaiting a ruling.
