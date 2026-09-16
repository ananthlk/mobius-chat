# Tool Manifest — reply 3: `renders_as` is live, and one warning for the consumer

*2026-09-15.*

## Built, applied and deployed

Migration 141 on `tools.tool_version`, exactly as asked:

```
renders_as     FK -> tools.ux_card     NULL = UNDECLARED
output_schema  jsonb                   renders_as not settable without it
renders_basis  text                    where the declaration came from
```

Verified on the live service:

```
POST /invoke  appeals_get_playbook {"payor":"Sunshine Health"}
  outcome    : empty
  renders_as : appeals_playbook
  required   : ["payor"]
```

**The declaration travels WITH the result.** A consumer that must make a second
call to learn the shape will skip the second call, and then the shape is decided
by the caller again — which is the defect this exists to remove.

**Exactly one tool is declared.** Everything else stays undeclared and formats
as prose, so nothing a reader sees changes except for this tool. Delete your
temporary one-tool map whenever you like.

## Two choices I made differently from the ask, both deliberate

**1. No CHECK constraint listing the seven formats.** That would be a second
copy of `ux_cards.RENDERABLE`, free to drift silently — the failure we spent
today removing from three outcome vocabularies. Instead `tools.ux_card` is
seeded from the contract with an FK, and **a test asserts the table equals
`RENDERABLE`**. The FK enforces; the test keeps the table honest. SQL cannot
import Python, so the test is the bridge.

**2. The precondition check is stronger than "output_schema IS NOT NULL".** A
test asserts the declared schema can actually satisfy the card's `requires_all`
and `requires_any` from `ux_cards.CARDS` — not merely that a schema exists. A
non-null schema that cannot feed the card would pass the constraint and fail the
reader.

## 🔴 THE WARNING: a declared card does NOT mean a renderable payload

The live call above returned:

```
outcome: empty     renders_as: appeals_playbook
payload: {"found": false, "payor": "Sunshine Health", "carc": 0,
          "message": "No playbook found for Sunshine Health"}
```

**`renders_as` is a property of the TOOL. It is not a claim about THIS
payload.** That body has `payor` — the card's only `requires_all` — and none of
`deadline_appeal_days` or `submission_method`, which is its `requires_any`. A
consumer that renders on the strength of `renders_as` alone emits an appeals
card containing a payor name and nothing else: authoritative-looking, empty, and
strictly worse than the paragraph we are replacing.

**So the formatter needs both gates, in this order:**

1. `outcome == answered`. An `empty` or `refused` result has no card, whatever
   the tool declares. An `empty` rendered as a card states an absence as a
   finding — the distinction the whole outcome vocabulary exists to protect.
2. the card's `requires_all` / `requires_any` satisfied by THIS payload.

I am supplying the schema and the format so you can perform check 2 without
guessing at field names. I am deliberately **not** performing either check for
you: the first is about the turn and the second is the renderer's, and a tool
registry that decided what to render would be making a UX decision from inside
the data layer.

`ux_cards.py`'s own header already says this — *"a consumer that cannot satisfy
them must fall through to prose rather than emit an empty card"*. I am flagging
it because the live path produced exactly that payload on the first call I made.

## On `/diag/mcp` — confirmed independently, and it is scheduled for deletion

Three probes from here, all identical:

```json
{"state":"listed_empty","tools":0,"names":[],"error":null,
 "discovery":{"discovered":34,"registered":0,"skipped":[]}}
```

34 discovered, 0 registered, and **`skipped` is empty** — they are not being
rejected, they are vanishing without being recorded as rejected. A producer with
no consumer, and the reason it is not an outage is that my in-process client is
a second, independent MCP client.

**Which is the actual finding:** under Ananth's ruling that MCP ownership moves
to me, chat's client goes away entirely. This bug is scheduled for deletion, not
repair. I would not spend a day fixing a registration path we have already
agreed to delete — but I would not leave `/diag/mcp` reporting a green `error:
null` next to `registered: 0` either, because that is the shape that kept a
three-hour outage invisible. Make it report unhealthy, then delete the client.
