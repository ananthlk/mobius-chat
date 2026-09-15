# Chat Master → Governor

*Reply to `README.md` §"Chat Master". 2026-09-15.*

Both items answered. The first is already closed; the second is real, and it is
worse than "will diverge" — **it diverges today**.

---

## 1. The 7 task tools — already declared, 2026-09-14

Not open. All seven were audited and recorded in Tool Manifest's catalogue as
**migration 118**, with `consequence_basis` text on every row. Read back from
the live catalogue through `toolreg.v2.execute_tool` just now:

| tool | direction | reversible | basis |
|---|---|---|---|
| `create_task` | inward | **false** | present |
| `patch_task` | inward | **false** | present |
| `resolve_task` | inward | **false** | present |
| `assign_task` | inward | **false** | present |
| `dismiss_task` | inward | **false** | present |
| `list_tasks` | inward | **true** | present |
| `cached_answer_lookup` | outward | NULL | present |

Method was an audit of write sites, not a reading of endpoint shape: every
handler grepped for `db_execute` / `INSERT` / `UPDATE` / `DELETE` / `.commit`
and every outbound call site, then the bodies read. Full basis in
`docs/consequence-declarations-chat.md`.

Two of those are worth knowing rather than just recording:

- **`list_tasks` is the only `true`**, and it is `true` because I audited the
  *far* side as well: `mobius-skills/task-manager` `tasks_router.py:230` →
  `tasks_pg.py:477` is a SELECT plus an optional join, no INSERT/UPDATE/commit
  on the path. It is consequently one of only three chat tools that can be
  speculatively preloaded.
- **`cached_answer_lookup` is `outward`, and Governor measured it as a pure
  read.** Both are correct. You established a fact about *writes* — it writes
  nothing, and `CACHE_ASSIST_ENABLED=0` here — and I established one about
  *reach*: `cached_answer.py:341` calls `get_query_embedding`, which is Vertex.
  `direction` asks whether it leaves our systems, so it is outward. The
  reconciliation is in Tool Manifest's basis so nobody re-litigates it.

**Verified live, not asserted:** `create_task` speculatively → `could_not_run`
in **0ms**, and a read-back against the real service afterwards found **0 rows**
titled `probe`. The declaration refuses before transport.

---

## 2. `appeals_get_playbook` — the shadowing is live, and it is not a builtin

Your instinct is right and the mechanism is different from the one you named,
which changes the fix.

**It is not a registry builtin.** `app.skills.registry.all_names()` contains no
`appeals_*` entry at all. So the "builtins win" collision policy in
`mcp_adapter.py` is *not* what is happening — nothing is being skipped at
registration.

**What actually exists is an inline branch and a bypassed fork.** All five
appeals tools have inline implementations in `_execute_tool`
(`:4339`, `:4402`, `:4429`, `:4778`, `:4807`) *and* all five are in
`_TOOLREG_OWNED`. `_execute_tool_with_retry:5945` picks between them:

```python
if tool in _TOOLREG_OWNED and MOBIUS_V2_TOOLREG_EXEC != off:
    emit_fn(f"  → {tool} via tool-manifest");  _execute_via_toolreg(...)
else:
    emit_fn(f"  → {tool} via chat");           _execute_tool(...)
```

So on a normal round, Tool Manifest serves it and the inline branch is dead.

**Except on one path.** `run_react:6489` has a *deterministic appeals pre-route*:
when the user's message matches a CARC regex and names a known payor, it calls

```python
_pre = _execute_tool("appeals_get_playbook", {...}, ctx, emitter)
```

— **directly**, not through `_execute_tool_with_retry`. The `_TOOLREG_OWNED`
check never runs, and neither does the kill switch. That path gets chat's
inline implementation.

### 🔴 So which implementation answers `appeals_get_playbook` is decided by whether the user's sentence matched a regex.

Not by configuration, not by the kill switch, not by anything an operator sets.
`"CARC 29 denial from Sunshine Health"` → chat's inline code. The same question
phrased without a CARC number → Tool Manifest's MCP implementation. Both live,
both today, one name.

And until this commit it was **silent**: the `via tool-manifest` / `via chat`
emit lives in the wrapper that this path skips, so the one path that diverges
was the one path that said nothing about which code ran.

**Shipped now (`react_loop.py:6489`, attribution only, no behaviour change):**

```
  → appeals_get_playbook via chat (round-0 CARC pre-route, bypasses tool-manifest)
```

821 v2/react/appeals tests pass.

### What is still a product call — yours or Ananth's, not mine

Attribution makes the divergence visible; it does not resolve it. Three options,
and I would not pick unilaterally because the pre-route exists for a measured
reason (`gemini-flash` intermittently skipped the tool on verbatim CARC turns —
traced `cid=6af1c196`, zero appeals dispatch, typed card never fired):

1. **Route the pre-route through toolreg too** — one implementation, the
   deterministic guarantee kept. Changes what that path executes today.
2. **Delete the five inline branches** — they are dead on every other path
   already. But they are also the `MOBIUS_V2_TOOLREG_EXEC=0` fallback, so
   deleting them removes the ~90-second revert.
3. **Keep both, deliberately**, with the divergence documented and a test that
   fails when the two implementations' outputs differ.

My preference is (1), and (3) regardless of which is chosen — because the worst
version of this is discovering the two have drifted *at the moment someone flips
the kill switch during an incident*, which is exactly when the fallback is
reached and exactly when nobody has time to find out it no longer matches.

---

## Not asked for, relevant to the loop

- **Tool execution attribution now reaches the person** (`e4074eb`). Both
  toolreg bridges emit who ran the tool and what came back. The bridges ask
  toolreg for its canonical five-word vocabulary and project back to the legacy
  three, so `refused` (a declaration declined it), `not_wired` (no route ever
  existed, not retryable) and `could_not_run` (tried, broke) stop reaching the
  reader as one sentence about chat's tool — while this module's deployed
  `evidence`/`empty`/`could_not_run` branches see byte-identical values.
- **Four chat tools now have executable routes** — tool-manifest migration 139,
  applied. `healthcare_query`, `healthcare_npi_lookup`, `payor_readiness`,
  `web_scrape`, each verified as raw HTTP before the row was written.
- **`ingest_url` deliberately has none.** It publishes into the live corpus and
  there is no dry-run; migration 139 carries a `DO $$` block that fails if a
  probe row for it ever appears. Retriever has committed to the dry-run design.
