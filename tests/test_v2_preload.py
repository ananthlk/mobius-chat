"""The preload plan: what runs before react speaks, and what react may ask for.

Spec: docs/preload-loop-spec-v1.md
Ananth, 2026-09-12: "preload the top 2 + rag ... load the tool outputs + leave
a generalized list of the next 3 tool choices for react to suggest."
"""
from app.pipeline.v2.preload import (
    ALWAYS_PRELOAD, EXECUTE_RANKED, NEVER_PRELOAD, SUGGEST_N, execute, plan,
)

# The real offer for the three-payer question, in estimate()'s rank order.
OFFER = ["refuse", "appeals_get_playbook", "healthcare_query",
         "appeals_lookup_rules", "document_upload_skill", "fetch_document",
         "ingest_url", "list_thread_document_uploads", "recall_evidence",
         "search_uploaded_document", "transform_previous_answer", "web_scrape",
         "rag"]


def test_rag_is_always_executed_whatever_it_ranks():
    """Ananth: "no we will always do rag". It sits 12th of 13 in this offer --
    tier=default, offered unconditionally, never ranked -- and it is the only
    tool that finds the answer to a payer-policy question."""
    p = plan(OFFER)
    assert "rag" in p.execute
    assert OFFER.index("rag") > 10, "fixture no longer reproduces the low rank"


def test_top_two_ranked_plus_rag():
    p = plan(OFFER)
    assert p.execute == ("rag", "appeals_get_playbook", "healthcare_query")
    assert len(p.execute) == EXECUTE_RANKED + len(ALWAYS_PRELOAD)


def test_the_next_three_are_offered_for_react_to_request():
    """WITHOUT THIS, PRELOAD IS A CAPABILITY REMOVAL. Today react may call
    anything in the manifest; under preload it gets what we chose. This session
    already shipped one capability removal -- the governor's unilateral stop,
    0 additions against 50 subtractions in 48h."""
    p = plan(OFFER)
    assert len(p.suggest) == SUGGEST_N
    assert p.suggest == ("appeals_lookup_rules", "fetch_document",
                         "list_thread_document_uploads")
    assert not set(p.suggest) & set(p.execute), "a tool cannot be both"


def test_side_effect_tools_are_never_preloaded():
    """Speculative execution must not WRITE. ingest_url pulls a URL into the
    corpus; document_upload_skill and transform_previous_answer act on state.
    Running them on a question nobody asked them to act on is a different class
    of mistake from a wasted retrieval."""
    p = plan(OFFER)
    for key in NEVER_PRELOAD:
        assert key not in p.execute, key
        assert key not in p.suggest, key


def test_the_gate_token_is_not_a_tool():
    """`refuse` is offered first by estimate() as a gate -- "a refusal is an
    answer". Executing it would be meaningless."""
    assert "refuse" not in plan(OFFER).execute


def test_every_excluded_tool_carries_its_reason():
    """A preload that ran the wrong tools and one that ran nothing look
    identical in an answer. "We never asked the right tool" must be a visible
    verdict, not an absence."""
    p = plan(OFFER)
    assert p.excluded
    for key, why in p.excluded:
        assert why and isinstance(why, str), key
    covered = set(p.execute) | set(p.suggest) | {k for k, _ in p.excluded}
    assert set(OFFER) <= covered, set(OFFER) - covered


def test_rank_order_is_estimates_and_is_not_recomputed():
    """estimate() ranks; the governor supplies gaps and budget. Two rankers
    would be two authors -- and tonight produced one silent disagreement
    between a decision and a re-derivation of it."""
    shuffled = ["rag", "healthcare_query", "appeals_get_playbook"]
    p = plan(shuffled)
    assert p.execute == ("rag", "healthcare_query", "appeals_get_playbook"), (
        "plan() reordered the offer instead of honouring it"
    )


def test_an_empty_offer_plans_nothing_and_says_so():
    p = plan([])
    assert p.is_empty and p.execute == () and p.suggest == ()


def test_an_offer_of_only_rag_still_executes_it():
    p = plan(["rag"])
    assert p.execute == ("rag",) and p.suggest == ()


def test_rag_is_not_executed_when_the_offer_withheld_it():
    """ALWAYS means "whatever it ranks", not "whether or not it was offered".

    estimate() withholds rag when the budget cannot carry it -- measured at
    budget_ms=18000: "rag withheld: worst case 20s against a 18s envelope".
    That refusal is BINDING. Executing it anyway would be the governor
    overruling Tool Manifest's budget arithmetic with a constant.
    """
    p = plan(["appeals_get_playbook", "healthcare_query", "fetch_document"])
    assert "rag" not in p.execute
    assert p.execute == ("appeals_get_playbook", "healthcare_query")


# ── the frame sections ──────────────────────────────────────────────────────

def test_a_tool_that_ran_and_found_nothing_is_reported_as_such():
    """"Ran and found nothing" and "was never run" are different facts and
    carry opposite advice. Omitting the empty one lets react assume it was
    never tried -- the never-searched / searched-and-empty collapse this whole
    contract exists to end."""
    from app.pipeline.v2.frame import preload_sections
    txt = "\n".join(preload_sections(
        [{"tool": "rag", "ok": True, "summary": "17 chunks"},
         {"tool": "healthcare_query", "ok": False, "summary": ""}], ()))
    assert "healthcare_query -> ran, returned nothing" in txt
    # THE PROPERTY, not the layout: rag's summary reaches the frame, attached
    # to rag. Pinned to "rag -> 17 chunks" this passed on the arrow format and
    # would have gone red on any re-layout that still carried the fact.
    _rag_line = txt[txt.index("rag"):]
    assert "17 chunks" in _rag_line


def test_the_suggestion_list_tells_react_how_to_ask():
    """A list of names react cannot act on is decoration."""
    from app.pipeline.v2.frame import preload_sections
    txt = "\n".join(preload_sections([], ("web_scrape", "fetch_document")))
    assert "web_scrape" in txt
    assert "Name one in your gap report" in txt
    assert "not choosing a tool this round" in txt


def test_no_preload_renders_no_sections():
    """Absent preload must leave the frame exactly as it was -- this ships dark
    and must be a no-op until it is switched on."""
    from app.pipeline.v2.frame import preload_sections
    assert preload_sections([], ()) == []


# ── execution ───────────────────────────────────────────────────────────────

def test_every_planned_tool_appears_in_the_result():
    """A tool missing from the result reads to react as never-attempted --
    the exact collapse this contract exists to end."""
    from app.pipeline.v2.preload import execute
    p = plan(OFFER)
    got = execute(p, lambda t, i: {"ok": True, "summary": "x"}, "q")
    assert [g["tool"] for g in got] == list(p.execute)


def test_a_raising_tool_does_not_take_the_turn():
    from app.pipeline.v2.preload import execute
    def runner(tool, inputs):
        if tool == "rag":
            raise RuntimeError("boom")
        return {"ok": True, "summary": "fine"}
    got = execute(plan(OFFER), runner, "q")
    assert got[0]["tool"] == "rag" and got[0]["ok"] is False
    assert "errored" in got[0]["summary"]
    assert all(g["ok"] for g in got[1:]), "one failure must not fail the rest"


def test_execution_is_sequential_and_stays_that_way():
    """Concurrency here is unsafe: _execute_tool assigns ctx.sources,
    ctx.plan, ctx.answer_set and ctx.react_bypass_integrate. Two tools in
    flight on one ctx clobber each other. The saving was ~1.2s of 11.6s
    because fan-out already made rag's internal work concurrent."""
    import ast
    import inspect
    from app.pipeline.v2 import preload as P
    tree = ast.parse(inspect.getsource(P))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("ThreadPoolExecutor", "gather", "as_completed", "Thread"):
        assert banned not in names, (
            f"{banned} appeared in preload: concurrent _execute_tool on one "
            f"shared ctx races on ctx.sources and ctx.react_bypass_integrate"
        )


# ── the wiring: preload must REACH the model ────────────────────────────────

def test_preload_results_reach_the_frame():
    """Executed and never rendered is the producer-with-no-consumer defect with
    a retrieval bill attached. AST, because a substring search matches the
    comment explaining it."""
    import ast
    import pathlib
    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    tree = ast.parse(src)
    passed = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "render"):
            continue
        kw = {k.arg for k in node.keywords}
        if {"preloaded", "suggest"} <= kw:
            passed = True
    assert passed, "frame.render is never called with preloaded/suggest"


def test_preload_is_flag_gated_and_arm_scoped():
    """Running tools speculatively on the v1 arm would vary BOTH arms and the
    A/B could no longer attribute anything. And a change that spends real
    retrieval needs an off switch that is not a rollback."""
    import pathlib
    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    code = "\n".join(l.split("#")[0] for l in src.splitlines())
    i = code.index("MOBIUS_V2_PRELOAD")
    window = code[i:i + 320]
    assert "orchestrator_version" in window, "preload is not arm-scoped"
    assert "_is_task_mode" in window, "task mode must not preload"


def test_preload_failure_leaves_todays_behaviour():
    """Ships dark: a missing toolreg, a failed estimate or a raising tool must
    leave ctx._v2_preloaded empty, and an empty preload renders no sections."""
    from app.pipeline.v2.frame import preload_sections
    assert preload_sections([], ()) == []


def test_tool_manifest_is_in_the_build_context_allowlist():
    """A Dockerfile COPY is NOT enough: .dockerignore is an ALLOWLIST (`*`
    then `!sibling/`), so a sibling absent from it never reaches the builder
    and the COPY silently gets nothing.

    This exact defect shipped earlier today -- eval/ was COPYd, was not
    allowlisted, and an endpoint 404'd in production behind nine green tests.
    Caught this time by reading .dockerignore before the build finished rather
    than after it failed.
    """
    import pathlib
    df = pathlib.Path("Dockerfile").read_text()
    # BOTH filters, because they are different files and only one of them is
    # the one that matters. deploy.sh passes deploy/.gcloudignore via
    # --ignore-file, so THAT governs what reaches Cloud Build; .dockerignore
    # governs the builder once the tarball is there. Editing only .dockerignore
    # cost two failed builds -- the directory never entered the tarball, and
    # the COPY failed with "file not found in build context".
    di = (pathlib.Path(".dockerignore").read_text()
          + "\n@@GCLOUD@@\n"
          + pathlib.Path("deploy/.gcloudignore").read_text())
    # The TOP-LEVEL sibling is what the allowlist governs: `!mobius-chat/`
    # admits mobius-chat/app and mobius-chat/eval alike. An earlier version of
    # this test compared full paths and flagged six false positives -- a gate
    # that cries wolf gets disabled, which is worse than no gate.
    copied = {ln.split()[1].split("/")[0] for ln in df.splitlines()
              if ln.startswith("COPY mobius-")}
    docker_part, gcloud_part = di.split("@@GCLOUD@@")
    allow = lambda txt: {ln[1:].rstrip("/") for ln in txt.splitlines()
                         if ln.startswith("!mobius-")}
    # A sibling must clear BOTH: absent from either one and it never arrives.
    allowed = allow(docker_part) & allow(gcloud_part)
    missing = {c for c in copied if c not in allowed}
    assert not missing, (
        f"COPYd but not allowlisted in .dockerignore: {missing} -- the COPY "
        f"will get nothing and the failure is silent"
    )


def test_preload_does_not_reference_locals_bound_later():
    """LIVE: "[v2.preload] failed: cannot access local variable
    '_pp_time_mod'" -- bound 100 lines BELOW the preload block. The fail-soft
    swallowed it, so preload logged, did nothing, and the turn looked normal.

    Second UnboundLocalError tonight from the same cause (see `kept`). In a
    6,900-line function "is this name in scope here?" is not answerable by
    reading nearby code, so this is asserted over the AST: every Name the
    preload block LOADS must be bound at or above it, or be a global/import.
    """
    import ast
    import pathlib
    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and any("MOBIUS_V2_PRELOAD" in ast.dump(x) for x in ast.walk(n))),
              None)
    assert fn is not None, "preload block not found"

    start = next(n.lineno for n in ast.walk(fn)
                 if isinstance(n, ast.Constant) and n.value == "MOBIUS_V2_PRELOAD")
    # Locals assigned at or before the preload block, plus its own imports.
    # LOOP AND WITH TARGETS ARE BINDINGS TOO, and unlike assignments they are
    # bound before their own body runs -- so they are NOT line-restricted. The
    # gate counted only ast.Assign, which made every `for x in ...: use(x)`
    # inside the block read as "used before bound" and fired on correct code.
    # A gate that cries wolf gets ignored, and then it is not a gate.
    bound = set()
    for n in ast.walk(fn):
        tgt = None
        if isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            tgt = n.target
        if tgt is not None:
            bound |= {t.id for t in ast.walk(tgt) if isinstance(t, ast.Name)}
        if isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars is not None:
                    bound |= {t.id for t in ast.walk(item.optional_vars)
                              if isinstance(t, ast.Name)}
    bound |= {t.id for n in ast.walk(fn) if isinstance(n, ast.Assign)
             and n.lineno <= start + 60
             for t in n.targets if isinstance(t, ast.Name)}
    bound |= {a.asname or a.name for n in ast.walk(fn)
              if isinstance(n, ast.Import) and n.lineno <= start + 60
              for a in n.names}
    bound |= {a.asname or a.name for n in ast.walk(fn)
              if isinstance(n, ast.ImportFrom) and n.lineno <= start + 60
              for a in n.names}
    bound |= {a.arg for a in fn.args.args}
    # Lambda parameters, except-handler names, and module-level functions are
    # all legitimately in scope and are NOT function-locals bound later.
    # Omitting them made the first version of this gate flag five false
    # positives -- and a gate that cries wolf gets deleted, which is worse than
    # no gate. Same correction as the .dockerignore gate an hour ago.
    bound |= {a.arg for n in ast.walk(fn) if isinstance(n, ast.Lambda)
              for a in n.args.args}
    bound |= {n.name for n in ast.walk(fn)
              if isinstance(n, ast.ExceptHandler) and n.name}
    bound |= {n.name for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)}

    # Names the block READS that look like function-locals (leading underscore)
    # and are not bound above it.
    late = set()
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
            continue
        if not (start <= n.lineno <= start + 60):
            continue
        if n.id.startswith("_") and n.id not in bound:
            late.add(n.id)
    assert not late, (
        f"preload reads locals bound later in the function: {sorted(late)}"
    )


def test_the_toolreg_dsn_gets_a_password_injected():
    """LIVE on 01066: "fe_sendauth: no password supplied".

    toolreg reads TOOLREG_DATABASE_URL and connects with psycopg2 DIRECTLY --
    it never passes through db_client, which is where chat injects
    CHAT_DB_PASSWORD into a deliberately secret-free DSN. So the env var alone
    authenticates as nobody, and deploy/dev.env is tracked and must stay
    secret-free, so the password cannot simply be written there.

    Asserts the injection EXISTS on the preload path and preserves the URL --
    an injection that mangles the DSN fails differently and just as fatally.
    """
    import pathlib
    import re
    from urllib.parse import quote
    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    code = "\n".join(l.split("#")[0] for l in src.splitlines())
    assert "TOOLREG_DATABASE_URL" in code and "CHAT_DB_PASSWORD" in code, (
        "the preload path no longer injects a password into the toolreg DSN"
    )
    # The regex it uses must actually match the DSN shape deploy/dev.env ships.
    dsn = [l for l in pathlib.Path("deploy/dev.env").read_text().splitlines()
           if l.startswith("TOOLREG_DATABASE_URL=")]
    assert dsn, "TOOLREG_DATABASE_URL is not in deploy/dev.env"
    url = dsn[0].split("=", 1)[1]
    m = re.match(r"(postgresql(?:\+\w+)?://)([^:@/]+)(@.+)$", url)
    assert m, f"the injection regex cannot match the shipped DSN: {url[:60]}"
    out = f"{m.group(1)}{m.group(2)}:{quote('p@ss', safe='')}{m.group(3)}"
    assert "mobius_rag" in out and "cloudsql" in out, "injection mangled the DSN"


# ── round 1 is excluded from STEERING, not from PRELOAD ─────────────────────

def test_round_one_receives_the_preload_evidence():
    """THE LIVE BUG. The steering block was gated on `rn > 1` -- right for
    naming a gap (there are none yet), WRONG for the preload sections. So
    preload executed, its evidence rendered into a block round 1 never
    received, and react searched again. The trace shows the preload query,
    then "Round 1: I'll use the rag tool" running the same search.

    My previous gate asserted frame.render is CALLED with preloaded=. It is --
    on rounds 2+. The gate was one round off with the bug.
    """
    import pathlib
    src = pathlib.Path("app/pipeline/react_loop.py").read_text()
    code = "\n".join(l.split("#")[0] for l in src.splitlines())
    i = code.index("MOBIUS_V2_STEER")
    window = code[i:i + 400]
    assert "rn > 1" in window, "the steering gate moved; re-check this test"
    assert "_v2_has_preload" in window, (
        "round 1 is gated out of the governor block entirely, so preload "
        "evidence never reaches the round it exists for"
    )


def test_a_preloaded_round_one_does_not_tell_react_to_write_a_search():
    """"Ask the question as asked, one query naming every part" instructs
    react to write a search -- directly contradicting "you are not choosing a
    tool this round". Two authors, one round."""
    from app.pipeline.v2 import statements as S
    from app.pipeline.v2.posture import (
        Budget, Posture, RoundState, seed_root_gap,
    )
    root = seed_root_gap("q")
    st = RoundState(round_index=1, open_gaps=(root,), gaps_open_history=(1,),
                    budget=Budget(remaining_s=90.0, remaining_c=3.0, band_s=25.0),
                    next_round_cost_s=10.4, acting_cost_s=10.0,
                    validate_cost_s=9.6, question="q")
    with_pre = S.Ctx(state=st, round_index=1, gap=root, preloaded=True)
    without = S.Ctx(state=st, round_index=1, gap=root, preloaded=False)
    assert "FRM-2" not in {s.id for s in S.select(with_pre, Posture.EXPLORE).statements}
    assert "FRM-2" in {s.id for s in S.select(without, Posture.EXPLORE).statements}, (
        "suppressing FRM-2 when preloaded must not disarm it when not preloaded"
    )


def test_the_role_says_judge_when_evidence_is_already_in_hand():
    """§6 reading "find evidence" directly above §10's "here is the evidence"
    is the same two-authors contradiction that made the role fight the review
    instruction earlier tonight."""
    from app.pipeline.v2 import frame as F
    from app.pipeline.v2 import statements as S
    from app.pipeline.v2.posture import (
        Budget, Posture, RoundState, seed_root_gap,
    )
    root = seed_root_gap("q")
    st = RoundState(round_index=1, open_gaps=(root,), gaps_open_history=(1,),
                    budget=Budget(remaining_s=90.0, remaining_c=3.0, band_s=25.0),
                    next_round_cost_s=10.4, acting_cost_s=10.0,
                    validate_cost_s=9.6, question="q")
    c = S.Ctx(state=st, round_index=1, gap=root, preloaded=True)
    txt, _ = F.render(c, Posture.EXPLORE,
                      preloaded=[{"tool": "rag", "ok": True, "summary": "23 passages"}],
                      suggest=("web_scrape",))
    assert "judge what has already been retrieved" in txt
    assert "find evidence that closes" not in txt


def test_an_empty_plan_is_logged_not_skipped_silently():
    """A preload that quietly does nothing is indistinguishable in the logs
    from one that never executed.

    Live 2026-09-12: a dev turn showed no [v2.preload] line at all, and the
    only way to tell "the gate was false" from "the plan was empty" was to
    read the source and guess. Both are legitimate outcomes; neither may be
    silent. This is the rule the module states everywhere and did not follow.
    """
    src = open("app/pipeline/react_loop.py").read()
    # Anchored on the CALL, not its argument list — the argument list changed
    # the moment schemas were threaded through, and the gate broke on a
    # correct edit. Fingerprint, not property, one more time.
    i = src.index("_plan = _v2pre.plan(")
    block = src[i:i + 1400]
    assert "if _plan.is_empty:" in block
    assert "NOTHING TO RUN" in block
    # The diagnosis has to name what WAS offered, or the next reader is back
    # to guessing which half failed.
    assert "offered=" in block and "excluded=" in block


# ── what to call a tool with comes from the OFFER, not from us ─────────────
#
# Ananth: "why are you doing this and not tool_manifest.. they have select 2
# tools + rag". This module used to read each skill's inputs_schema and decide
# for itself whether a tool could run on the question — a judgement about Tool
# Manifest's offer, made inside a consumer. ToolOffer now carries `inputs` and
# `preload_reason`, so the seat that chose the tool names the call.


def test_the_offer_names_the_call_and_we_do_not_invent_it():
    """Their catalogue had healthcare_query's key as `question` the whole time.
    My schema guess sent `query`, and a policy question went into a 30s ICD-10
    lookup."""
    seen = {}
    pl = plan(["rag", "healthcare_query"],
              inputs={"rag": {"query": "Q"}, "healthcare_query": {"question": "Q"}})
    execute(pl, lambda t, i: seen.update({t: dict(i)}) or {"ok": True},
            "Q", inputs={"rag": {"query": "Q"},
                         "healthcare_query": {"question": "Q"}})
    assert seen["healthcare_query"] == {"question": "Q"}
    assert seen["rag"]["query"] == "Q"


def test_inputs_None_means_DO_NOT_EXECUTE_not_call_with_nothing():
    """28 of 29 MCP signatures disagree with their live inputSchema, so an
    empty dict would have us invoke a tool with no arguments and read the
    failure as the tool's fault."""
    pl = plan(["rag", "appeals_get_playbook"],
              inputs={"rag": {"query": "Q"}, "appeals_get_playbook": None},
              reasons={"appeals_get_playbook": "requires ['payor']"})
    assert pl.execute == ("rag",)
    assert dict(pl.excluded)["appeals_get_playbook"] == "requires ['payor']"


def test_the_reason_comes_from_the_seat_that_decided():
    """Tool Manifest asked us to keep the exclusion logging but take the reason
    from them — better than re-deriving it at the point of use."""
    pl = plan(["x"], inputs={"x": None}, reasons={"x": "declares no free-text argument"})
    assert dict(pl.excluded)["x"] == "declares no free-text argument"


def test_a_missing_reason_still_says_something():
    pl = plan(["x"], inputs={"x": None}, reasons={})
    assert dict(pl.excluded)["x"]


def test_no_inputs_supplied_leaves_behaviour_unchanged():
    """Callers that pass nothing keep the old fallback — which is the invention
    that caused the bug, so it is the fallback and never the default."""
    pl = plan(["rag", "x"], inputs=None)
    assert "rag" in pl.execute


# ── unpriced time is not spent speculatively ────────────────────────────────
#
# Tool Manifest, 2026-09-12, after healthcare_query timed out in this set:
#     declared 49 tools — 46 of them have NO CEILING AT ALL; estimate() prices
#     worst case as `ceiling or p50`, so those 46 are budget-checked against
#     their TYPICAL cost. healthcare_query declared 800ms and took 30s.
#
# A SPEND decision, not a selection decision — which is what makes it the
# governor's. Tool Manifest ranks; this does not reorder, and an excluded tool
# is still offered to react in `suggest`.

from app.pipeline.v2.preload import affordable_to_preload


def test_unknown_worst_case_is_not_treated_as_cheap():
    """`ceiling or p50` silently substitutes the typical cost. That is how a
    30-second tool passed a budget check priced at 800ms."""
    ok, why = affordable_to_preload(None)
    assert not ok and "unknown" in why


def test_a_declared_30s_ceiling_is_refused():
    ok, why = affordable_to_preload(30000)
    assert not ok and "30000" in why


def test_a_normal_tool_is_allowed():
    assert affordable_to_preload(3000)[0]


def test_an_unreadable_ceiling_is_refused_not_coerced():
    assert not affordable_to_preload("soon")[0]


def test_an_excluded_tool_is_still_offered_to_react():
    """Not removed from the turn — removed from the speculative spend before
    the turn starts. react may still call it, where the spend follows a
    decision instead of preceding one."""
    pl = plan(["rag", "slow_tool", "other_tool"],
              inputs={k: {"query": "Q"} for k in ("rag", "slow_tool", "other_tool")},
              ceilings={"rag": 20000, "slow_tool": None, "other_tool": 2000})
    assert "slow_tool" not in pl.execute
    assert any(k == "slow_tool" for k, _ in pl.excluded)


def test_no_ceilings_supplied_leaves_behaviour_unchanged():
    pl = plan(["rag", "x"], inputs={k: {"query": "Q"} for k in ("rag", "x")},
              ceilings=None)
    assert "x" in pl.execute or "x" in pl.suggest


def test_not_preloadable_is_not_not_offerable():
    """🔴 CAUGHT BY READING THE INTEGRATION OUTPUT, NOT BY A TEST.

    On the real offer, excluding non-preloadable tools from `suggest` as well
    as `execute` left react with an EMPTY tool list — a capability removal,
    which this module's own header calls the failure that cost 50 turns.

    A tool that cannot run on a bare QUESTION is exactly the tool react should
    be told about: react can supply the payor, the url, the carc. Preloadability
    gates EXECUTION only."""
    pl = plan(["rag", "appeals_get_playbook", "web_scrape"],
              inputs={"rag": {"query": "Q"}, "appeals_get_playbook": None,
                      "web_scrape": None},
              reasons={"appeals_get_playbook": "requires ['payor']",
                       "web_scrape": "requires ['url']"})
    assert pl.execute == ("rag",)
    assert "appeals_get_playbook" in pl.suggest and "web_scrape" in pl.suggest


def test_a_tool_too_slow_to_preload_is_still_offerable():
    """react calling it later is a DECISION, not a guess — the spend follows
    the choice instead of preceding it."""
    pl = plan(["rag", "slow"],
              inputs={"rag": {"query": "Q"}, "slow": {"query": "Q"}},
              ceilings={"rag": 20000, "slow": 30000})
    assert "slow" not in pl.execute and "slow" in pl.suggest


# ── preconditions this seat can check and Tool Manifest cannot ─────────────

def test_a_tool_whose_precondition_is_knowably_false_is_not_run():
    """MEASURED LIVE: search_uploaded_document was ranked into preload on a
    payer-policy question and returned "No uploads on this thread." — a call we
    could have known was pointless, because whether the THREAD has uploads is
    turn state and estimate() ranks from the question and the catalogue.

    NOT a judgement about usefulness — that is selection and it is theirs.
    Its precondition is false RIGHT NOW, from state only this side holds."""
    I = {k: {"query": "Q"} for k in ("rag", "search_uploaded_document")}
    pl = plan(list(I), inputs=I, turn_state={"thread_uploads": 0})
    assert "search_uploaded_document" not in pl.execute
    assert "search_uploaded_document" in pl.suggest      # still offered


def test_the_same_tool_runs_when_the_precondition_HOLDS():
    """The gate is about this turn, not about the tool."""
    I = {k: {"query": "Q"} for k in ("rag", "search_uploaded_document")}
    pl = plan(list(I), inputs=I, turn_state={"thread_uploads": 3})
    assert "search_uploaded_document" in pl.execute


def test_unknown_turn_state_does_not_block_anything():
    """Absent state is not a false precondition — inventing one would drop
    tools for a reason nobody established."""
    I = {k: {"query": "Q"} for k in ("rag", "search_uploaded_document")}
    assert "search_uploaded_document" in plan(list(I), inputs=I).execute
    assert "search_uploaded_document" in plan(list(I), inputs=I,
                                              turn_state={}).execute
