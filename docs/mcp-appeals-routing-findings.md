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

---

## 11. The unifying statement — chat has no failure detection it owns

Platform's framing, and it deserves to be one sentence rather than three findings:

> **Every mechanism chat uses to decide whether a remote tool failed is a value the
> remote service controls.**

| # | mechanism | who controls it | how it fails |
|---|---|---|---|
| 1 | `getattr(result, "isError", False)` — `mcp_manager.py:127` | the **server**, via a wire flag | absent/renamed attribute reads as success |
| 2 | `text.startswith("Unknown skill")` — `react_loop.py:2792` | the **server**, via its error wording | any other phrasing passes |
| 3 | the appeals server's `isError` | the **server**, and it sets `False` on a real failure | flag is present and wrong |

All three fail **open**. There is no fourth mechanism holding the line, and
`SkillEnvelope.success` — the field introduced to be that mechanism — defaults to
`True` and is never set by `registry.py:312`.

**Why "the server should set `isError`" is true but not sufficient** (Platform's
point, and the reason #3 matters more than it first appears): the flag *is* the
thing that is wrong, so nothing that trusts it can help. Fixing the appeals server
fixes one server. **Chat has to stop trusting a flag a peer service controls, in
exactly the way `:2792` has to stop matching a string a peer service controls.**
Same lesson, two mechanisms, and the fix for both is the same shape — derive the
outcome from something chat owns, and treat a remote-supplied value as evidence
rather than as a verdict.

That is what makes fail-closed a **correctness** requirement rather than a
diagnostic convenience, and it is why it holds even if §4 is never explained:

> 🔴 **Fixing the routing alone would not have fixed the symptom.** Route the
> appeals calls to the correct server and the identical pathology reappears with a
> different error string — a real backend timeout, wrapped as a `SourceRef` citing
> itself, recorded by the funnel as a success.

## 12. §4 stays open — deliberately, and permanently if need be

Platform proposes not writing the join up as closed **even if the `getattr` lead
becomes provable**. Agreed, and adopted.

What is genuinely established: the *server-changed* escape is dead (primary
unredeployed since `2026-09-08T13:20Z`, before the 14:12 / 14:14 / 14:35Z rows;
`isError True` consistently; same-SDK comparison came out identical rather than
revealing a difference). `getattr` failing open is the only explanation still
standing and it fits matching text, constant server, and production's `completed`
line.

**"Only explanation standing" is not "confirmed."** Three seats have now each named
a cause and been wrong at least once in this thread — the manifest example text,
routing-alone, `tool_agent` as the caller, and my own "retry guard is spared."
**The packet is worth more with one honest unknown than with a fourth cause that is
inferred.** It stays open until the image or the log line settles it.

## 13. The two standing lessons from this thread

1. **Distrust log text that explains rather than observes.** *"Builtins win; MCP
   tool not registered"* asserted a false cause while accurately reporting a true
   event. Three seats read the assertion and skipped the event.
2. **Verifying that a branch exists is not verifying that it runs.** Three seats
   independently confirmed the appeals hardening at `:3073` and none asked whether
   control reaches it. It does not. `gate_with_no_caller`, applied to a dispatch
   branch.

---

## 14. §4 REOPENS — the SDK lead is dead across the whole pin range

Platform (`aebd873`) argued against their own hypothesis. Both of their
measurements reproduce here, and I closed the one escape they had left open.

**(a) `isError` fail-closed at the `getattr` is inert.** `CallToolResult.isError`
is a real pydantic field with `default=False`, so an omitted flag is materialised
**before any `getattr` runs**:
```
server omitted isError -> r.isError = False
  getattr(r,'isError',False) = False
  getattr(r,'isError',True)  = False   <- the "fail closed" fix changes nothing
  'isError' in r.model_fields_set = False   <- the only discriminator
```
**Changing the default at `mcp_manager.py:127` would look like a fix and do
nothing.** Platform sent this before I wrote it rather than after, which is the
whole value of the exchange.

**(b) The primary sends the flag explicitly**, so the default never fires there:
```
mobius-provider-roster-credentialing/mcp
   isError          = True
   explicitly sent? = True   ('isError' in model_fields_set)
```

**(c) NEW — and it kills the last variant of the lead: the field exists at the pin
floor.**
```
mcp == 1.0.0   fields: ['content', 'isError']   has isError: True
mcp == 1.2.0   has isError: True
mcp == 1.26.0  has isError: True
```
`requirements.txt:113` allows `mcp>=1.0.0`. **Every version in the allowed range
carries the field**, and the server sends it explicitly as `True`. So any SDK the
image could legally contain parses `isError=True` → `mcp_manager` returns
`(text, False)` → failure branch → **no sources attached.** But sources are
observed.

**The SDK explanation cannot account for the observation at any permitted version.**
Withdrawn. §4 is now *less* explained than when we agreed to keep it open, and
keeping it open was the right call for exactly this reason.

### What remains, stated as classes rather than a guess

1. **The deployed image does not match commit `299519c`.** Platform and I both
   verified `git show 299519c:app/services/mcp_manager.py` — that is the **commit**,
   and **nobody has verified the image**. `scripts/deploy.sh dev` builds the
   **working directory, not committed HEAD** (documented in
   `feedback_chat_deploy_cache_bug`), and this checkout is shared by several
   sessions. An image built while any tree was dirty contains that tree.
2. **A path none of us has found** produces the `MCP: <tool>` `SourceRef`. Weak —
   `mcp_adapter.py:274` is the only producer repo-wide, grepped without the `app/`
   restriction — but it is not excluded.

(1) is now the leading candidate, and it is our own tooling rather than an
upstream quirk. **This also changes what the log line must capture:** recording
`success` and `isError` alone cannot distinguish "the code did something
unexpected" from "the code is not the code we think is running." It should also
record the running module's identity — e.g. `mcp_manager.__file__` plus the
deployed `GIT_SHA`/revision — so the next reading answers *which code ran*.

### Consolidated (1), amended again

pin `mcp` to the **deployed** version → `isError` checked via
**`'isError' in result.model_fields_set`**, *not* a `getattr` default →
`_skill_success` structured, not string → `registry.py:312` sets `success=False`.

### And the appeals server's fail-open is its own defect

It sends `isError=false` **explicitly** (`in model_fields_set = True`) on a real
failure — not omitting the field and being defaulted. **It asserts that a failure
succeeded.** Appeals' fix, independent of chat. This does not change §11: chat must
still stop trusting the flag, because a server that lies explicitly defeats
`model_fields_set` too.

---

## 15. 🔴 Fixing the misroute in isolation would INCREASE the failure rate

Appeals found the mechanism behind the CARC 197 timeout (`9e94a7f`, via Platform).
It is not a slow backend: their MCP server is mounted in the same FastAPI app and
its tools call **the service's own REST endpoints over HTTP from inside a request
handler**. `/rules/197` answers in 0.149s against a 30s timeout — a 30-second
timeout fired on a 150-millisecond endpoint.

Verified here independently against the deployed service (their file:line claims
are their reading; their repo is not checked out here):

```
template minScale      = ABSENT        <- scale-to-zero
template maxScale      = 2
service  minScale      = ABSENT
containerConcurrency   = 80            <- thread-pool starvation, not deadlock
APPEALS_AGENT_SELF_URL = UNSET -> falls back to localhost
```

A service whose handlers call themselves, capped at 2 instances, scaling to zero.
Under concurrent load a request occupies a worker while waiting on another worker
of the same pool. That is why it is intermittent, why a `curl` and an MCP probe
measured opposite things and both were right, and why CARC 197 tips first at
~3.4× the payload of CARC 29.

**The sequencing consequence, and it is sharper than my own "routing alone would
not fix the symptom":**

> **The misroute has been accidentally suppressing the load that triggers their
> bug.** Every appeals call has been going to the primary and dying as
> `Unknown tool` — so the appeals service has received almost none of this traffic.
> **Correctly routing those calls delivers, for the first time, the exact load
> pattern that starves it**, and the resulting failures will read as a regression
> introduced by the routing fix.

So Appeals' two — internal handlers called directly, `isError=True` on failure —
go **before or with** the routing work. Neither is blocked by chat's hold. Chat's
(1) is unaffected and still first.

### Consolidated order

1. **chat** — pin `mcp` to the deployed version → `isError` via
   `'isError' in result.model_fields_set` (**not** a `getattr` default) →
   `_skill_success` structured, not string → `registry.py:312` sets `success=False`
2. **appeals** — internal handlers called directly; `isError=True` on failure
3. **chat** — both doors: appeals branch above `:2759` **and** `tool_agent:902`
4. **chat** — origin URL carried on `SkillSpec`
5. **chat** — `signal` + the self-citing `SourceRef`

### A config doing undeclared safety work

`fleet.yaml:89` has appeals at `{mode: standby}`. A service whose handlers call
themselves is incompatible with standby/scale-to-zero, so that setting is holding
off a latent defect without saying so. **Same class as `mobius-payor`'s `min: 1`
holding off `payer_context` degradation** — see
`project_payer_context_silent_degradation`. Raised as fleet-power, not chat, and
not acted on here.

### Third standing lesson, from Appeals

**"I had the fact and drew the smaller conclusion."** They had already described
their own fail-open to the Tool Manifest seat — as a *parsing hazard for the
caller*, missing that it was a *protocol violation on their end*. Not a wrong
fact: a correct fact scoped to the wrong owner. Distinct from §13's two, and the
hardest of the three to catch, because nothing about it looks like an error.

---

## 16. ✅ §4 CLOSED — the image runs mcp **2.2.0**, where the field is `is_error`

I read the deployed image directly. No docker needed: Artifact Registry's REST API
serves manifests and blobs to `gcloud auth print-access-token`, so the layers can be
pulled and opened with `tarfile`. No API enablement, no build, no deploy.

### First: candidate (1) is dead — the image DOES match the commit

Extracted from layer 7 of `sha256:ca0695e4…9327a` and diffed against `299519c`:

```
mcp_manager.py : IDENTICAL to 299519c   (isError check present at :127)
mcp_adapter.py : IDENTICAL to 299519c
react_loop.py  : IDENTICAL to 299519c   (:2792 guard as analysed)
registry.py    : IDENTICAL to 299519c   (:116 success=True, :313 literal)
```

So the deploy-from-dirty-tree hypothesis — which I called the leading candidate —
is **wrong**. Worth stating plainly: I proposed it, and reading the artefact killed
it in one step where four seats had reasoned about it for hours.

### Then: the actual cause

`requirements.txt:113` says `mcp>=1.0.0`. **The image resolved that to `mcp 2.2.0`.**
Platform and I between us tested 1.0.0, 1.2.0 and 1.26.0 and concluded the field
was present "at every version the pin permits." **We tested the versions we chose,
not the version that is deployed. `>=1.0.0` permits 2.x.**

In 2.2.0 the type moved out of `mcp` into a separate `mcp_types` package, and the
field was renamed:

```python
# mcp_types/_v2026_07_28/__init__.py
is_error: Annotated[bool | None, Field(alias="isError")] = None
```

`isError` survives only as a **serialization alias**. Pydantic v2 does not expose an
alias as an attribute. Reproduced under the image's exact SDK:

```
mcp == 2.2.0
CallToolResult module: mcp_types._types
fields: ['meta','content','structured_content','is_error','result_type']

server sent isError=True  ->  r.is_error = True
  hasattr(r,'isError')       = False
  getattr(r,'isError',False) = False    <-- what mcp_manager.py:127 evaluates
  => branch taken: SUCCESS (success=True)
```

**That is the whole join.** The server flags the error correctly; the client asks for
an attribute that no longer exists; `getattr` returns its default; `mcp_manager`
returns `(text, True)`; `mcp_adapter` takes the success branch and attaches a
`SourceRef` citing the error text; `_skill_success` passes because the text guard
tests a different literal; the span records `success`. Every observation accounted
for — matching text, the `completed` log line, the attached source, the span.

### 🔴 The scope is far wider than appeals

`getattr(result, "isError", False)` is **unconditionally `False` in production**. It
does not depend on the tool, the server, or the error. **Every MCP tool call in the
deployed service is recorded as a success, always — all 34.** Appeals is simply where
it became visible, because that is the tool whose calls always fail.

This retro-justifies §10 and widens it: `tool.dispatched:<tool>:success` carries **no
information at all** for MCP tools in this revision. Not "unreliable" — constant.

### Both proposed fixes were inert, and the second was caught the same way

Platform's amendment — `'isError' in result.model_fields_set` — **also fails**, for a
new reason:

```
model_fields_set          = {'content', 'is_error'}
'isError'  in fields_set  = False    <-- the proposed check
'is_error' in fields_set  = True     <-- the one that works
```

`model_fields_set` holds **field names**, not aliases. So the amended fix would have
shipped and changed nothing, exactly like the `getattr`-default fix before it. **Two
inert fixes in one thread, both caught by testing against the deployed artefact
rather than a local assumption.**

### Corrected fix (1)

1. **Pin `mcp` to `2.2.0`** — the version actually deployed. `>=1.0.0` spanning a
   major version is the root enabler; a silent major bump renamed a field the code
   depends on and nothing failed loudly.
2. **Read the outcome via the model, not a string attribute** — `result.is_error`
   under 2.x. Do not reach for `getattr` with a default on a field whose name is an
   API contract; if compatibility across 1.x/2.x is wanted, resolve it explicitly and
   fail **closed** when neither name is present.
3. `_skill_success` structured, not string; `registry.py:312` sets `success=False`.

### The lesson, and it is mine

I excluded a correct hypothesis by testing it against artefacts I picked. Platform
raised `getattr` fail-open early; they withdrew it and **I told them it was
"excluded at every permitted version."** It was right the whole time. The check that
would have settled it on the first pass is the same one that settled it now: **read
the thing that is running.** A version range is not a version, a commit is not an
image, and a local venv is not production.

---

## 17. The appeals image, read the same way — and the seam stated exactly

Platform caveated their own `40` because they read it in **chat's** venv, not the
appeals image. Correct caution, and the same technique that closed §16 resolves it —
no docker, no Container Analysis API, no build.

**Installed in the deployed appeals image** (`…mobius-appeals-prototype@sha256:51b7b29a…`):

```
anyio      4.15.1     (Platform read 4.13.0 in chat's venv)
starlette  1.6.0      (Platform read 1.0.0)
fastapi    0.141.1
httpx      0.28.1
uvicorn    0.52.4
mcp        1.30.0     <-- see below
```

Every library differs from the local reading, which is exactly why the caveat was
worth making. **The `40` survives it** — from `anyio/_backends/_asyncio.py` in that
image:

```
CapacityLimiter(40)
```

So the predicted knee at **~40 concurrent sync tool calls** against
`containerConcurrency: 80` is now measured against the artefact that is running,
not inferred from a local default. Platform's two sharpenings stand unchanged: the
limiter is **process-wide**, so a burst of MCP tool calls starves unrelated sync
endpoints in that process, and the load test remains Appeals' to run against a
non-production instance.

### 🔴 The seam, stated exactly

```
chat    runs mcp 2.2.0   -> CallToolResult.is_error   (isError only an alias)
appeals runs mcp 1.30.0  -> CallToolResult.isError
```

**The two services are on different MAJOR versions of the same protocol library.**
That is the whole failure in one line: the appeals server, on 1.x, sets `isError`
exactly as FastMCP's contract requires — and chat, on 2.x, asks for an attribute
that its own SDK renamed. **Neither service is wrong about the protocol. They are
wrong about each other.**

Both depend on `mcp` with no upper bound, so the two ends of one wire drifted a
major version apart with nothing failing loudly. This is the strongest possible
argument for §16's fix (1): **pin `mcp` on both sides**, and treat the version skew
between services that speak to each other as a thing that must be declared rather
than resolved independently by two `pip install` runs months apart.

It also sharpens §11 one last time. Chat must derive the outcome from something it
owns — and "something it owns" cannot include a field name in a dependency that a
peer service resolves separately.

---

## 18. The asymmetry — and its evidence status, stated honestly

Platform's addition: appeals **bounded** the major (`mcp[cli]>=1.0,<2`); chat did
not (`requirements.txt:113`, `mcp>=1.0.0`). **Only chat crossed.** So "pin `mcp` on
both sides" is right, but the fix should not read as symmetric blame — chat's
omission is the one that caused it, and appeals' constraint is the one chat should
have copied.

**Evidence status, because this thread's own lesson applies to it:**

- **Chat's side is verified directly** — `mcp>=1.0.0` at `requirements.txt:113`,
  read here.
- **Appeals' `<2` bound is Platform's read of their repo**, which is not checked out
  here. I tried to confirm it from the deployed image and **could not**: the appeals
  image (Cloud Run source deploy) carries no `requirements.txt`, `pyproject.toml` or
  `Dockerfile` in any layer I pulled — 5,435 files across its app and site-packages
  layers, no build manifest among them.
- **Corroborating, not confirming:** the appeals image has `mcp 1.30.0` installed.
  An unbounded `mcp>=1.0` resolving at its 2026-09-08 build would have taken the
  latest — chat's build took 2.2.0. Landing on the newest **1.x** is what a `<2`
  bound produces. That is consistent with Platform's read and is not a substitute
  for it.

**Recorded as corroborated rather than verified**, which is the same distinction
this thread spent a day learning. The fix does not depend on it: pin both sides
regardless.

## 19. Closing note — what actually made this work

Appeals' observation, kept in their framing rather than paraphrased:

> *the productive move wasn't either of us being right, it was both of us
> publishing reasoning that could be checked.*

Five causes were named and withdrawn — the manifest example text, routing-alone,
`tool_agent` as the caller, "the retry guard is spared", and the `getattr` lead
(withdrawn, then wrongly excluded by me, then confirmed correct). **None was found
by the seat that filed it.** Two proposed fixes were inert and neither was written.

The tally is a property of the exchange, not of any seat. It worked because every
seat reported against its own hypothesis at least once, and because the reasoning
was published in a form the others could execute rather than merely read.

---

## 20. The asymmetry is REAL after all — proved by resolution timing, not by reading a file

Platform withdrew §18's asymmetry after finding the retained build source
(`gs://run-sources-mobius-os-dev-us-central1/…`) whose Dockerfile reads
`pip install … "mcp[cli]>=1.0"` — **no upper bound**. They flagged, correctly, that
the newest retained source is `2026-08-11T20:51Z` and there is no September source,
so **what they read is not the build that produced the running image.**

That caveat is the whole answer. **They withdrew a correct claim on the strength of a
source artefact that is not what is running** — the same error class this entire
thread is about, in its last paragraph.

### The proof, from PyPI upload times and the image's own build stamp

```
mcp 1.30.0  first uploaded  2026-09-07T14:34:14Z
mcp 2.2.0   first uploaded  2026-09-07T16:06:19Z   (1h32m later)
mcp 2.0.0   first uploaded  2026-07-28T13:45:28Z

appeals image created       2026-09-08T14:08:27Z   (revision 00218-zah)
  -> installed: mcp 1.30.0
```

**At the moment the running appeals image was built, `mcp 2.2.0` had been on PyPI for
22 hours.** An unbounded `mcp[cli]>=1.0` resolves to the newest compatible release —
that is how chat's build got 2.2.0. Appeals' build landed on **1.30.0, the newest
1.x**. A resolver does not choose the second-newest by accident.

**No transitive cap explains it:** scanning every `.dist-info/METADATA` in the appeals
image, **nothing declares a dependency on `mcp` at all** — so no other package
constrained it. The bound could only have come from the install command itself.

**Conclusion: the build that produced the running appeals image DID carry an upper
bound**, even though the 08-11 source did not. The image tag — **`pintest-20260908`**,
i.e. *pin test*, dated the build day — fits exactly: the pin was added on 09-08, in
source that was never retained.

**One alternative I cannot exclude:** if `mcp 1.30.0` were already present in the base
image or an earlier layer, `pip install "mcp[cli]>=1.0"` would treat the requirement
as satisfied and not upgrade, producing 1.30.0 with no bound. I did not pull the base
layers to test it. Given 1.30.0 was one day old at build time, a base image carrying
it is unlikely — but it is unexcluded, and Appeals can settle it in one sentence.
Platform has already asked them.

### What this does and does not change

- **The fix does not move**: pin `mcp` on both sides, chat's is the one that crossed.
- **The attribution is restored**: appeals bounded the major in the build that is
  running; chat did not. Chat's omission is the cause.
- **§18's evidence status was right and is now upgraded** — from *corroborated* to
  *established by resolution timing*, by a method that reads the running artefact
  rather than a file describing an older one.

**And the pattern repeats one level up.** Platform's other observation stands and is
sharpened by this: the appeals service's dependency set is **not reconstructible from
anything durable** — no manifest in the image, no September source in the bucket, and
the newest source does not match the running artefact. The only record of what it
depends on is the installed packages in one image: a **description, not a
specification**. That is the same defect as `mcp>=1.0.0` one level up — *the thing
that would have told us what was running was never written down* — and it is why the
correct claim was retractable at all.
