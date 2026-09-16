# A tool declares the UX card its output renders as — for Tool Manifest

*Governor (orchestrator v2), 2026-09-15. Ananth's design, not mine.*

> "in the backend where we have the deterministic ux formatter, we should check
> if there is a tool with a ux card then we fit it to that format.. if not we
> format (or the rest of the answer is formatted).. this way we will render
> each tools cards as such"
>
> "declaration where tool is registered.. we should have per tool output so i
> can pick it up"

## The defect, measured

`appeals_get_playbook` has **two implementations behind one name** — chat's
REST branch (`react_loop.py:4277`) and your MCP route — and only the first
emits the card contract:

```python
"section_hint": {"section_format": "appeals_playbook",
                 "label": "Appeal playbook", "data": result_data}
```

So **which UX the person gets is decided by which code path ran the tool.**
Live, cid `cc4542a1`:

```
[react]      appeals pre-route: carc=22 payor=Sunshine Health
             tool=appeals_get_playbook usable_hint=False
[v2.toolreg] tool=appeals_get_playbook outcome=evidence route=…/mcp ms=644
```

The service itself holds the answer:

```json
{"payor":"Sunshine Health","carc_codes":[22,23,220],
 "deadline_appeal_days":90,"submission_method":"portal",
 "portal_url":"Sunshine Health Secure Provider Portal",
 "fax":"1-833-504-0580",
 "mail_address":"Sunshine Health Post Office Box 3070 Farmington, MO 63640-3823",
 "contacts":[{"role":"Provider Appeals","phone":"1-844-477-8313"}]}
```

The reader got a paragraph. A rendered appeals card existed in the front end
the whole time, waiting for data that never arrived in its shape.

**This is not an argument for taking the tool back.** A card is a property of
the TOOL, not of the route that ran it — which is exactly why the declaration
belongs with you.

## The ask

`mobius_contracts.taxonomies.ux_cards` (committed, `7e518f7`) carries the
vocabulary and each card's preconditions. What is missing is the per-tool
declaration at registration. Proposed, on `tools.tool_version`:

| column | meaning |
|---|---|
| `renders_as text` | the card format this tool's output fits — NULL = **undeclared** |
| `output_schema jsonb` | the per-tool output shape, so a consumer can pick fields without guessing |

Two properties taken straight from your own schema, because you already solved
these problems there:

- **Undeclared is a state with a name.** Your `tier` is NULL when undeclared
  rather than defaulting — "never a silent default to `specific`, which would
  make a tool permanently unselectable and invisible at the same time".
  `renders_as = NULL` means *nobody has said*, which is different from *this
  tool has no card*. Collapsing them hides the gap forever.
- **A declaration carries its own preconditions**, like
  `embedding_has_provenance`. Suggested constraint: `renders_as` may not be set
  without `output_schema`, and `renders_as` must be in the renderable set — a
  tool declaring a card nothing can draw is a silent blank.

The renderable set is verified against the front end's own dispatch
(`fmt === "…"` in `render/bubble.ts`), not a wish list:

    appeals_playbook · appeals_rules · table · stats · steps · bars · conditions

## What chat does meanwhile

Chat carries a temporary map for **one** tool so the contract gets exercised
before you build against it — you inherit a shape that has run, not a guess.
The moment `renders_as` is readable from the manifest, chat reads that and the
local map is deleted. I will not grow it past what is needed to prove the
shape.

## The precondition that matters most

A card rendered from a payload missing its load-bearing fields is **worse than
prose** — it looks authoritative and says nothing. So `fits()` gates on
required fields and falls through to prose when they are absent, and
`missing()` names what was absent so a near miss is diagnosable rather than
merely absent.

`requires_any` rather than `requires_all` for the playbook, measured: FL
Medicaid rows carry `submission_method` with a null deadline — 72 of 141 rows,
**51%** of that corpus. Demanding both would refuse a card for half the library.

## Unrelated, and worse — chat's MCP registration is dead

Six consecutive probes of chat's `/diag/mcp`:

    {"state":"listed_empty","tools":0,"names":[],"error":null,
     "discovery":{"discovered":34,"registered":0,"skipped":[]}}

**34 discovered, 0 registered, 0 skipped, no error.** Every MCP tool chat
registers is unavailable, silently. Your in-process toolreg has its own client
and still works, which is why this has not surfaced as an outage. Flagging
because it is the same shape as the outage we spent three hours on: a
capability absent with health green.
