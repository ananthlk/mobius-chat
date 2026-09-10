# The appeals tools' "bypass MCP" path is unreachable in production

**Author:** Payor Policy Agent · **Date:** 2026-09-10
**Verified against:** deployed `mobius-chat-00977-8bl` (digest `sha256:ca0695e4…9327a`), live logs, live DB.
**Status:** findings only. **No code changed** — chat build is on hold pending Ananth.

Confirming the routing report from **Platform (mobius-c2, the platform /
product-awareness seat)** — referred to as "Platform" throughout — and adding the
mechanism they could not see
from outside. Their two independently-confirmed facts join, but **not through the
link either of us proposed.**

## 1. Confirmed — the routing defect is real

Discovery merges every `EXTRA_MCP_URLS` server into one list
(`mcp_adapter.py:465-474`); `_spec_from_mcp_tool` keeps only
`name`/`description`/`inputSchema`, so **the origin URL is dropped at
registration**. The single dispatch site is `call_mcp_tool(tool_name, _inputs)`
and `_get_mcp_url()` (`app/services/mcp_manager.py:56`) takes no tool name — it
returns the constant `MCP_SERVER_URL`. Every extra-server tool is called against
the primary. Reproduced through the real code path:

```
resolved url: …mobius-provider-roster-credentialing…/mcp
success = False
text    = 'Unknown tool: appeals_lookup_rules'
```

## 2. 🔴 NEW — the hardcoded appeals path is dead code in production

`react_loop.py:3070` says of the five appeals tools:

> *"These 5 tools bypass MCP and call the appeals REST API directly so the router
> treats them as Tier 1 (same weight as rag)."*

**That branch is at line 3073. The universal skill-registry fallback is at line
2758** — guarded only by `_skill_registry.has(tool)`, with no exclusion for
appeals — **and it returns.** Its own comment calls it the fallback for "MCP +
builtin skills *not handled above*", but appeals is handled *below* it. Once the
name is in the registry, **`_execute_tool` never reaches line 3073.**

Production logs confirm the name is in the registry, from the extra server:

```
14:10:27.730  register_mcp_skills: registered 5 MCP tool(s) as skills:
              appeals_lookup_rules, appeals_get_playbook, appeals_find_carc, …
14:10:27.967  register_mcp_skills: skipping MCP tool 'appeals_lookup_rules' —
              a skill with that name is already registered.
              Builtins win; MCP tool not registered.
```

**"Builtins win" is misleading.** There is no builtin *skill* by that name —
`app/skills/` contains none; the appeals handling is the hardcoded `_execute_tool`
branch, which is not in the registry at all. The thing already holding the name is
the **prior MCP registration** from the other server, exactly as `:492` allows
("Builtin **or prior MCP registration**"). So the skip is order-dependent between
two MCP registrations, and the winner is whichever server was polled first.

**Chain:** appeals registers as an MCP skill → `has()` is true at `:2758` →
registry dispatch wins → `call_mcp_tool` → routed to the **primary** (§1) →
`Unknown tool` → cited. The Tier-1 REST path the comment describes never runs.

## 3. Confirmed — the symptom is current, and it is not only the citation

21 source rows carry the error text, 8 today. Three of those turns ran **after**
`00977-8bl` began serving (09:09:05 CT): rows at 09:12:02, 09:14:00, 09:35:49.

It is not just the `SourceRef`. The span layer agrees:

```
tool.dispatched   appeals_lookup_rules:success
tool.result       appeals_lookup_rules:no_sources
```

`_skill_success = env.success and …` (`react_loop.py:2792`), so **`success=True`
propagated through the whole result dict**, not merely into the citation. The
planner, the retry guard and the funnel all saw a successful call.

## 4. 🔴 STILL OPEN, and now bounded to one line

Platform asked how `success` became True when `mcp_manager.py:127` checks
`isError`. **I could not close it either, and I am not asserting a chain I cannot
prove.** What I did establish narrows it to a single line:

- Production logs `MCP tool appeals_lookup_rules **completed**` — the
  `logger.info` on the **success** return, after the `isError` branch. No
  "returned error" warning anywhere in the window.
- The same tool, same primary URL, same function, from this tree, returns
  `success=False` and logs "returned error". The server **does** set `isError`.

So the deployed image and this source tree **do not behave identically at
`mcp_manager.py:127`**, even though the check is present in the deployed commit
`299519c` at that exact line and the working tree is clean.

**Ruled out** (checked, not assumed): a second producer of the `MCP: <tool>`
source — `mcp_adapter.py:274` is the only one; a snake_case field mismatch —
`CallToolResult.isError` exists with default `False` in both mcp 1.26.0 and 1.2.0;
`db_client.py`'s REST MCP path — different service (mobius-db-agent), not this
call.

**Not ruled out:** `requirements.txt:113` pins **`mcp>=1.0.0`, unpinned**, so the
image's SDK is whatever its build resolved. This repo has already been bitten by
exactly this — `698fd0e`, *"tolerant stream-tuple unpack for streamable_http_client
version drift"*. **Pinning `mcp` is worth doing regardless of whether it is the
cause here**, and it is the cheapest way to make this reproducible.

Independently of the cause, `getattr(result, "isError", False)` **fails open**: a
result object that does not carry the attribute is read as success. For an error
flag that default is backwards.

## 5. Correcting my own bound from this morning

I wrote that `ReactRetryGuard._is_zero_result` "is NOT affected — spared by the
attached source." **The fact was right and the conclusion I drew from it was
half the picture.** The self-citation (`SourceRef(text=text[:300],
document_name=f"MCP: {tool}")` — the answer citing its own output) means
`len(sources) > 0` is *always* true on the success branch. I reported that as
protective. It is equally the reason the guard **can never fire for any MCP
tool** — an erroring or empty MCP result never counts as a zero-result, so
`consecutive_failures_per_tool` never increments and tool-exhaustion never trips.
Platform has it right. Same mechanism, and I enumerated only the safe direction.

That is also why 16 turns could cite `Unknown tool` without anything intervening.

## 6. Sequencing, if the hold lifts

1. **Pin `mcp`** and make `isError` fail **closed**. Smallest, and it makes §4
   reproducible instead of arguable.
2. **Move the appeals branch above `:2758`**, or exclude those five names from the
   registry fallback. Restores the Tier-1 REST path the comment already promises.
3. **Carry the origin URL** on the `SkillSpec` and make `_get_mcp_url` take a tool
   name. This is the fix Platform asked for; note it is *not* sufficient alone —
   with §2 unfixed, appeals would still take the MCP path rather than REST.
4. Then the `signal` single-value defect (`0031fd4`) and the self-citation.

**Order matters:** fixing routing (3) first would make appeals calls *succeed*
against the appeals server, which would look like a fix while leaving the Tier-1
path still dead and the self-citation still inflating groundedness to 100%.

---

## 7. CORRECTION, later 2026-09-10 — `react_loop` is **not** excluded as the caller

Platform and Appeals concluded, from Appeals' reading, that *"the five tools **are**
hardened at `react_loop.py:3073` — unconditional set membership, no fall-through —
so react_loop cannot be the caller,"* and moved to `tool_agent.py` as the
explanation. **The premise is true and the conclusion does not follow.**

The set membership at `:3073` is unconditional **if you reach it**, and on this path
you do not. Proven by AST rather than reading:

```
registry-fallback `if _skill_registry.has(tool):`   lines 2759-2873
returns inside that block:                          [2859, 2873]
last stmt of block:                                 Return at line 2873
appeals branch `if tool in {...}`                   lines 3073-3614
  after registry block?                             True
  nested inside registry block?                     False
```

**The registry-fallback block ends in an unconditional `Return` at `:2873`.** The
appeals branch begins at `:3073`, after it and not nested inside it. So whenever
`_skill_registry.has("appeals_lookup_rules")` is true, `_execute_tool` returns at
`:2873` and **`:3073` is never reached.** The hardening is real and unreachable —
which is the same shape as the finding in §2, not a counter to it.

**And `has()` is true on every instance, by either branch of the log.** Where the
log says *"registered 5 MCP tool(s) as skills: appeals_lookup_rules, …"*, the name
was just registered. Where it says *"skipping … a skill with that name is **already
registered**"*, the name was already held. Both outcomes leave `has()` true.

**There is no builtin appeals skill for it to be held by.** `grep -rn "appeals_"
app/skills/` excluding `mcp_adapter.py` returns nothing. The only registrations of
these names are MCP ones, so the holder is always an MCP registration and dispatch
always lands on `mcp_adapter` → `call_mcp_tool` → the primary.

**What this changes:** `tool_agent.py` is a genuine second unhardened door — its
`:902` is the same bare `_skill_registry.has(hint)` with no appeals interception —
and it is worth fixing. But it is **not needed to explain the caller**, and
treating it as *the* caller would leave the main chat path unfixed. Both doors
route to the registry; the hardening at `:3073` protects neither.

This does not disturb Platform's fix ordering, but it does change what "fix the
door" means: moving the appeals branch above `:2759` (or excluding those five names
from the registry fallback) is required in `react_loop` **as well as** hardening
`tool_agent`.

**Unchanged and still open:** `success=True` on an `isError=True` response. Platform
is right that it needs one log line at `mcp_adapter.py:223` recording `success` and
`getattr(result, "isError", None)`. **I have not added it — chat builds are on hold
pending Ananth**, and I will not open that door on a peer's request.

---

## 8. PROBE RESULTS, 2026-09-10 — one hypothesis dies, one new defect appears

Ran Platform's `scripts/platform/probe_mcp_tool.py` (`c62129a`) inside chat's own
`.venv`, against both servers.

**Primary** (`CHAT_SKILLS_MCP_URL`, roster/credentialing):
```
advertises 29 tool(s)   — appeals_lookup_rules NOT among them
call_tool('appeals_lookup_rules', {'carc':'197'})
  isError  True                      <- attribute PRESENT, value True
  text     'Unknown tool: appeals_lookup_rules'
```

**Appeals prototype** (the `EXTRA_MCP_URLS` entry):
```
advertises 5 tool(s)  — all five appeals tools
call_tool('appeals_lookup_rules', {'carc':'197'})
  isError  False                     <- on a REAL ERROR
  text     '[appeals_lookup_rules] Error fetching rules for CARC 197: timed out'
```

### 🔴 NEW — the appeals server itself fails open

**It returns `isError: False` on a genuine backend failure**, putting the error in
the body text. This is independent of everything above and it changes the fix plan:

> **Fixing the routing alone would send appeals calls to a server that reports its
> own errors as success.** `call_mcp_tool` would return `(error_text, True)` → the
> success branch → an error wrapped as a `SourceRef` named
> `MCP: appeals_lookup_rules`, citing the timeout text as evidence. **The exact
> symptom we are trying to remove would survive the routing fix**, with a
> different error string.

So `isError` must fail **closed**, and the adapter must not treat it as sufficient
on its own — not merely to explain §4, but because a server we route to in
production demonstrably does not set it. This is now the strongest argument for
sequencing (1) ahead of (3), stronger than the groundedness point I made earlier.

It also means Appeals' timeout is real and separate: the tool is reachable and
its backend is timing out on CARC 197 right now.

### The "server changed" explanation is dead

The primary has **not been redeployed since 2026-09-08T13:20Z**
(`mobius-provider-roster-credentialing-00094-5dl`), well before today's rows at
14:12 / 14:14 / 14:35Z. Its `isError: True` has been constant throughout. So the
primary did not transiently behave differently.

### What still stands, and what it now rests on

`success=True` with text `'Unknown tool: appeals_lookup_rules'` — that text is
**exactly** the primary's, so the call did reach the primary, and the primary set
`isError: True` on the wire. The client did not see it. **The only surviving
explanation is that the deployed image's `mcp` SDK does not expose `isError` where
`mcp_manager.py:127` reads it**, so `getattr(result, "isError", False)` returns its
default and the code logs `completed`. That fits every observation: matching text,
constant server, production's success-path log line.

**I could not confirm the image's SDK version.** There is no Cloud Build record for
the chat image (`gcloud builds list` shows deep-research and payor only — chat is
built by `scripts/deploy.sh` locally), and docker is unavailable here. Confirming it
needs either the image itself or Platform's proposed log line at
`mcp_adapter.py:223`. **Still an unconfirmed lead, not a conclusion.**

When it is pinned, pin to **the version in the deployed image**, not to latest —
Platform's point, and correct: pinning forward would fix the drift and destroy the
evidence in one commit.

---

## 9. §4 CLOSED on the second half — and the guard is not belt-and-suspenders

**Finding is Appeals'**, relayed via Platform (mobius-c2), verified here.

`react_loop.py:2792`:
```python
_skill_success = env.success and bool(env.text and not env.text.startswith("Unknown skill"))
```
Measured, not read:
```
actual text            : 'Unknown tool: appeals_lookup_rules'
startswith("Unknown skill") -> False
guard passes           -> True
```

Their framing is the right one and sharper than "off by one word": **`registry.py:312`
emits `Unknown skill: {name!r}.` — chat's own literal, which this guard matches
exactly and always has. Chat's own `Unknown tool` (`react_loop.py:3619`) sets
`success=False` and `sources=[]`, so it never reaches the guard.** What the guard
cannot match is a *remote server's* wording. `'Unknown tool: <name>'` belongs to the
roster/credentialing MCP server. **It is a guard whose contract is a string owned by
another system**, and renaming `skill`→`tool` would leave it armed against exactly
two spellings.

### 🔴 And it is the ONLY mechanism, not a second line of defence

The comment at `:2791` calls it belt-and-suspenders. It is not:

```
SkillEnvelope.success default        : True   (registry.py:116)
registry.py:312 unknown-skill envelope: text=..., signal=no_sources  — no success=False
resulting .success                   : True
```

So for the unknown-skill case **the string comparison is the sole thing that turns a
miss into a failure.** The `success` field exists precisely to end this — its own
docstring (`registry.py:121-122`) says *"previously failure and success envelopes
were indistinguishable by shape, so react_loop.py's success check had to (wrongly)
infer from text."* The field was added; **`registry.py:312` was never migrated to
it.** A producer added, one consumer left on the old contract — the same shape as
everything else in this file.

### Consequence for fix (1) — I accept Platform's amendment

My (1) was "pin `mcp` + make `isError` fail closed." That closes fail-open #1 and
leaves #2 armed. Folding in: **`_skill_success` must read a structured outcome, not
a string**, and `registry.py:312` must set `success=False`. Otherwise the next MCP
server that words its error differently reproduces this entire thread — and the pin
would have made it *less* visible, not more.

Complete path to `success=True` with no single bug: SDK drops/renames `isError` →
`getattr(..., False)` reads no-error → the text guard written to catch exactly that
tests a literal owned by a different system → passes → `_skill_success` True →
propagates to the result dict, the span, and the funnel. **Three fail-open defaults,
each individually defensible.** That is why three seats hunted one bug and none of us
found it.

## 10. Correction I owe on my own earlier work — span-based conclusions

Platform flagged that appeals span history is unusable as a baseline until this
lands. **The same applies to my own MCP analytics finding** (schematic §3.2,
`70082ce`), and I should say so before someone builds on it: its table cites
`tool.dispatched → success` for `get_market_size`, `get_top_orgs` and
`get_market_share_timeseries`. **For MCP tools that column is exactly the field
this thread proves unreliable.**

**The conclusion still stands, but not on that evidence.** It rests on the answer
content — 2,923,378 beneficiaries, $793,099,275.81 paid, named organizations with
revenue — read from `chat_turns.final_message`, which no fail-open default can
manufacture. A tool that returned nothing cannot produce those figures. **`emitted`
is also sound**, since it records the planner's selection before any dispatch.

So: §3.2's *selection* finding is unaffected, and its *dispatch-succeeded* column
should be read as unverified for MCP tools until fix (1) lands. Corrected in the
schematic rather than left for a reader to discover.
