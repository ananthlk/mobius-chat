# The appeals tools' "bypass MCP" path is unreachable in production

**Author:** Payor Policy Agent · **Date:** 2026-09-10
**Verified against:** deployed `mobius-chat-00977-8bl` (digest `sha256:ca0695e4…9327a`), live logs, live DB.
**Status:** findings only. **No code changed** — chat build is on hold pending Ananth.

Confirming Platform's routing report, and adding the mechanism they could not see
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
