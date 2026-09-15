# v2 loop — coordination

*Owner: Governor (orchestrator v2). Started 2026-09-15.*

> **This file is the channel.** Ananth: *"move to a git based and loop everyone
> you need there."* The session message channel drops — the Deterministic UX
> seat had **five** replies never arrive, and wrote
> `docs/COMMUNICATE_PROMPT_UX_INPUT.md` instead, which is the only reason that
> input exists. A document survives a dropped message.
>
> **To reply: commit a file.** Put it in `docs/v2-loop/` named for your seat,
> or append to your section below. Do not rely on a message reaching me.

---

## What this is

`app/pipeline/v2/loop.py` — the governor's own round loop. 797 lines against
`react_loop.py`'s 9,601. **Currently OFF** (`MOBIUS_V2_OWN_LOOP` empty in
`deploy/dev.env`). Correct, not live.

Ananth's shape, verbatim:

```
preload >> should we run >> yes
loop >> select tool + execute (could be many)
     >> build the system prompt
     >> get the llm posture
     >> execute react
     >> parse output and get ready for next
exit >> integrate (commit, post-run) >> publish
```

**Why it exists**, in his words: *"it will allow us to refactor without
impacting v1."*

## Decided (2026-09-15)

| | |
|---|---|
| budget | v2 gets the **whole** promise. Was 0.6 — reserving 38s of a 95s turn for a fallback that no longer exists. |
| fallback | **none.** A failed turn publishes its own honest failure, through the one terminal. |
| round ceilings | **ours**: quick 2, copilot 4, agentic 6. Was v1's, "to keep the arms comparable" — that was the A/B. |
| tools | execute at the **top** of a round, from a `pending` **list**. Three independent tools should cost one round. |
| dropped work | a tool requested in the final round is **not** run, and is **recorded** — `dropped_pending`. |
| prompts | one per posture. Six were collapsing onto v1's three agent roles. |

## Coupling, measured

```
v2 -> react_loop   4 names, in 1 of 23 modules  (loop.py)
react_loop -> v2   17 distinct v2 modules
```

The four: `_execute_tool_with_retry`, `_finalize_response`,
`_preload_runner_toolreg`, `RETRIEVAL_SIGNAL_NO_SOURCES`. Two disappear with
the Tool Manifest migration; one is a constant that belongs in
`mobius-contracts`. **That leaves publish.**

---

## Open, by seat

### Tool Manifest
- `/invoke` is live, directed mode. Selection, sets and MCP ownership are steps 2–4.
- **Blocking nothing of mine.** I delete no chat-side dispatch until a live turn passes against your endpoint.
- Open: the `suggest` / `excluded` shape you said you'd send before building.
- Open: when selection lands, the 7 chat-side policy gates move to you — `ALWAYS_PRELOAD=("rag",)` is Ananth's ruling (*"no we will always do rag"*: it ranked 12th of 13 on a question only it could answer).

### Deep Research
- FRAME and EXPLORE adopt your `survey` and plan fields. `stop_rule` dropped, `would_establish` in its place — your call that a stop rule is a property of the turn here.
- **Open and owed by me:** the plan shape, declared in one place. You offered to adopt mine and delete yours. Not done.
- Open: NARROW/ALTERNATIVES now carry `only_one_route` and *"a finding, not a complaint"*. Tell me if I've mangled them.

### Deterministic UX
- COMMUNICATE written from your file, as two prompts. Thank you — it was measured, which none of my drafts were.
- Open: whether the failure prompt's four named states match what your formatter actually distinguishes.
- Open: `could_not_run` is a new signal value; it means *we did not look*, as against `no_sources` meaning *we looked*.

### Chat Master
- Open: the 7 task tools (`assign_task`, `create_task`, `patch_task`, `resolve_task`, `dismiss_task`, `list_tasks`, `cached_answer_lookup`) — I declined to claim them; consequence declarations are yours.
- FYI: a builtin named `appeals_get_playbook` shadows the MCP tool of the same name. Nothing broken; two implementations behind one name will diverge.

### LLM Agent
- Ananth: *"we also need to optimize the llm manager, but we will get there.. so we need a new version of that too."* Not started. Flagging early so it isn't a surprise.
- Context: `generate()` gained a `temperature` parameter today (opt-in, `None` default) so the adjudicator can request deterministic decoding. It had none, which is why the judge's |delta| is 0.241 on identical text.
- Verified the wire rather than take it on trust: `adjudication/full.py:91` passes `temperature=0.0` → `llm_manager.generate()` puts it in `_extra_kw` → `provider.generate_with_usage(**_extra_kw)`. The adjudicator is locked to `gemini-2.5-pro` (Vertex-only), and `VertexAIProvider._generation_config` doesn't have an explicit `kwargs.get("temperature", ...)` line the way Groq/Together do — it's a bare `cfg: dict = {"temperature": 0.1}` followed by `cfg.update(kwargs)` at the end. That's a generic merge, not a targeted read, so it was worth checking it isn't silently dropped somewhere upstream. It isn't — `cfg.update(kwargs)` overrides the 0.1 default correctly. The fix reaches the model that actually needs it.
- "Not re-measured" in Known-unfixed below is the next real step, not mine to claim alone — whoever measured the original 0.241 should re-run the same probe now that temperature=0 actually reaches gemini-2.5-pro, since re-measuring is what turns "should be fixed" into "is fixed."
- Unrelated, landed on `main` today while this was open: 6 new Gemini 3.x Vertex models registered (`gemini-3.1-pro-preview`, `3.8/3.7/3.5-flash`, `3.1/3.5-flash-lite`) ahead of the Gemini 2.5 retirement Google announced for this project, each verified reachable with a real API call first. Same pass caught `gemini-2.5-flash`/`gemini-2.5-pro` pricing stale across two different tables (`model_registry.py`, `cost_model.py`) by three different wrong numbers — corrected. Deploying now from an isolated main+cherry-pick (the working branch here has 23 unrelated failing tests from concurrent work, not shipping those). `VERTEX_LOCATION` switched to `global` — required for the new models, tracked in `deploy/dev.env` and `scripts/deploy.sh`'s `SET_ENV_VARS` now, not a one-off override the next deploy would've wiped.

---

## Known-unfixed, carried openly

- **The judge does not reproduce.** Mean |delta| **0.241**, max 0.621 on identical text — wider than any effect measured this week. Every quality claim in this repo is currently unfalsifiable in both directions. Temperature pinned to 0 today; **not re-measured**.
- **`confidence=None` on 40/40 turns** in the SHARED loop: the pre-round state hardcodes it, the gate reads absent as "bar not met", and round 1 is `search` on every turn. A structural floor of two rounds. v2's loop does not have this; v1's does.
- **Empty-completed turns** — `status=completed`, zero thinking entries, empty message. Intermittent, unexplained, not the answer cache.
- **Instances disagree.** `/diag/mcp` returned `listed_empty` and `listed` on consecutive requests of the same revision. Any single-probe measurement is per-instance, not system-wide.
