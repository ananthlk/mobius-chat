"""The A/B harness endpoint — shape assertions against the FE render contract.

These assert the CONTRACT, not the implementation: the FE builds against this
shape and a silent change to it breaks a page in another seat's repo.
"""

import inspect
import pathlib

from app.api import ab_harness as ab


def test_the_routes_the_contract_names_exist():
    """An EXACT set: adding a route to the FE's contract must be a deliberate
    edit here, never a side effect."""
    paths = {f"{list(r.methods)[0]} {r.path}" for r in ab.router.routes}
    assert paths == {
        "POST /ab/runs", "GET /ab/runs",
        "GET /ab/runs/{run_id}", "GET /ab/runs/{run_id}/q/{qid}",
        "POST /ab/runs/{run_id}/q/{qid}/arm/{arm}",
    }, paths


def test_the_envelope_comes_from_the_SNAPSHOT_not_the_ttld_endpoint():
    """/chat/response is a Redis key with a TTL that returns
    {"status":"processing"} forever once it expires -- absence dressed as a
    plausible value. A comparison you cannot re-open is not infrastructure."""
    import ast
    # AST, not a string search: my own comment in this function EXPLAINS why we
    # do not read /chat/response, and a substring check matched the explanation.
    # Third time tonight a test of mine has read prose instead of the program
    # (a purity regex matched "random" inside a docstring saying the module uses
    # none; an .index() found a module docstring). The question is what the code
    # CALLS, not what the text contains.
    tree = ast.parse(inspect.getsource(ab.get_comparison))
    literals = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any("/chat/response" in v for v in literals), \
        "box 1 must not read the TTL'd endpoint"
    assert "answer_envelope" in inspect.getsource(ab.get_comparison)


def test_the_verdict_is_READ_never_recomputed():
    """A posture-vs-posture diff at render time is a SECOND MAPPING, and it
    disagrees with compare() exactly where compare() is most interesting: on
    `extend`, which resolves by reason rather than by name."""
    src = inspect.getsource(ab._trace)
    assert "shadow_verdict" in src
    for forbidden in ("== r[\"posture\"]", "!= r[\"posture\"]", "if posture ==", "diverge\" if"):
        assert forbidden not in src, f"verdict looks recomputed: {forbidden}"


def test_there_is_no_precomposed_divergences_array():
    """One source. A second array carrying facts already in decision_trace
    drifts the first time someone edits one and not the other."""
    src = inspect.getsource(ab.get_comparison)
    assert "divergences" not in src


def test_arms_is_keyed_and_supports_one_arm():
    """Columns = number of arms. A 1-arm run is valid and useful: baseline
    capture, and full-width re-read. N arms need no new mode."""
    src = inspect.getsource(ab.get_comparison)
    assert 'arm_list = [{"id": run["arm_a_id"]' in src
    assert 'if run.get("arm_b_id")' in src, "arm_b must be optional"


def test_experiment_block_carries_held_constant_and_varied():
    """Rule 5 as DATA -- rendered as the header of every comparison. A
    comparison without it stated is a shape with no experiment behind it."""
    src = inspect.getsource(ab.get_comparison)
    assert '"held_constant"' in src and '"varied"' in src


def test_harness_flag_is_always_true_including_single_arm():
    """A baseline capture still is not production. 'Nobody was served' holds
    for a one-arm run too."""
    for fn in (ab.get_run, ab.get_comparison):
        assert '"harness": True' in inspect.getsource(fn)


def test_kept_is_none_not_false_when_unknown():
    """null renders '—', never 'false'. Unset is not false -- four
    PipelineContext fields with falsy defaults hid a missing producer for a
    month on exactly this."""
    src = inspect.getsource(ab.get_comparison)
    assert "else None" in src and '"kept"' in src


def test_uses_the_logical_db_key():
    assert ab._DB == "chat"


def test_only_create_reads_the_set_file():
    """The file is a CREATE-time input, never a read-time one.

    Over the AST, not the text: a run's questions come from its frozen
    snapshot, so editing eval/<set_id>.json cannot rewrite what a past run
    asked. Mutation-checked by pointing get_run at _load_set -- this fails.
    """
    import ast, pathlib
    src = pathlib.Path("app/api/ab_harness.py").read_text()
    tree = ast.parse(src)
    callers = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
               and n.func.id == "_load_set":
                callers.add(fn.name)
    assert callers == {"create_run"}, callers


def test_create_persists_the_snapshot():
    """question_set must be in the INSERT column list -- a snapshot that is
    decided and not written is the `overran` defect again."""
    import ast, pathlib
    tree = ast.parse(pathlib.Path("app/api/ab_harness.py").read_text())
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "create_run")
    sql = " ".join(n.value for n in ast.walk(fn)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert "INSERT INTO ab_runs" in sql and "question_set" in sql


def test_capture_refuses_an_incomplete_turn():
    """409 and NOTHING written. A row with a null envelope is
    indistinguishable from a turn that answered with nothing."""
    import app.api.ab_harness as H
    from fastapi import HTTPException
    writes = []
    H._q = lambda sql, p=None: [{"run_id": "r1"}] if "from ab_runs" in sql else []
    H._x = lambda sql, p=None: writes.append(sql)
    import app.api.chat as C
    orig = C.get_chat_response
    orig_q, orig_x = ab._q, ab._x
    C.get_chat_response = lambda cid: {"status": "processing"}
    try:
        try:
            H.capture("r1", "q01", "v1", H.Capture(correlation_id="c1"))
            raise AssertionError("should have raised")
        except HTTPException as e:
            assert e.status_code == 409
        assert writes == [], writes
    finally:
        C.get_chat_response = orig
        H._q, H._x = orig_q, orig_x


def test_capture_reads_the_envelope_through_the_same_function_the_ui_does():
    """AST: capture() calls chat.get_chat_response, not a second envelope
    builder -- a private copy drifts from the renderer the first time either
    side changes."""
    import ast, pathlib
    tree = ast.parse(pathlib.Path("app/api/ab_harness.py").read_text())
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "capture")
    calls = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "get_chat_response" in calls
    assert not any("envelope" in c and "get_chat" not in c for c in calls), calls


def test_round_duration_is_written_AND_read():
    """The column that had no writer, wired end to end.

    `delivered_latency_s` and `delivered_cost_c` sat in turn_rounds through 100
    rows with zero populated -- declared here, never wired. An unwritten column
    reads as a legitimate negative. This asserts all three ends of the
    replacement exist: the enrichment sets it, the ledger's INSERT names it,
    and the endpoint returns it. Any one of the three missing = the same defect.
    """
    import pathlib
    orch = pathlib.Path("app/pipeline/orchestrator.py").read_text()
    ledg = pathlib.Path("app/pipeline/v2/ledger.py").read_text()
    api = pathlib.Path("app/api/ab_harness.py").read_text()
    assert '_row["round_duration_s"] = ' in orch, "no producer"
    assert "round_duration_s," in ledg and '"dur_s": r.get("round_duration_s")' in ledg, "not written"
    assert api.count('"round_duration_s": r["round_duration_s"]') == 2, "no consumer"
    # ...and the two writerless columns are gone from the turn_rounds
    # statements specifically. turn_attestations has its own delivered_cost_c,
    # a DECLARED absence with a documented reason (065: "STEP 3, not yet
    # measured") -- a different thing from a column nobody wired.
    import re
    for f, src in (("ledger", ledg), ("ab_harness", api)):
        for stmt in re.findall(r"[^\"]*turn_rounds[^\"]*", src):
            assert "delivered_cost_c" not in stmt, (f, stmt[:80])
            assert "delivered_latency_s" not in stmt, (f, stmt[:80])


def test_no_field_in_the_payload_claims_a_unit_it_does_not_carry():
    """Chat FE, 2026-09-11: `cost_usd` was reading `delivered_cost_c` — a
    CENTS column. Null on every row, so a $0.012 turn would have rendered as
    "1.2" the first time a real value landed and nothing before that would
    have looked wrong.

    Asserted as a PROPERTY over the source, not as a check for that one field:
    a `_usd` name may never take its value from a `_c` column, in either
    direction. The next mismatched pair fails here rather than shipping.
    """
    import re
    src = pathlib.Path("app/api/ab_harness.py").read_text()
    sql_free = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))
    for m in re.finditer(r'"(\w+)":\s*\w+\.get\("(\w+)"\)', sql_free):
        field, column = m.group(1), m.group(2)
        if field.endswith("_usd"):
            assert not column.endswith("_c"), f"{field} <- {column}: cents as USD"
        if field.endswith("_cents"):
            assert column.endswith("_c") or "cent" in column, f"{field} <- {column}"


def test_mismatch_kind_separates_the_two_mismatches():
    """Chat FE was about to fill a "v2 chose ___" slot from `prompt_mismatch`.
    That field is PROSE for a human; the name lives in `posture`.

    And the two mismatches are different events, which one prose field could
    not tell them apart:

        mis_prompted   v2 RAN the round, on v1's remediation prompt instead of
                       a gathering one — not like-for-like on that round
        not_generated  v2 chose ALTERNATIVES, v1 cannot produce that content,
                       so it shipped without it — the person saw a normal
                       answer, and crediting v2 with it would be crediting an
                       answer nobody saw

    Derived from stored fields, one rule, one author. Inferring it from the
    prose on the page would make the renderer the second.
    """
    import app.api.ab_harness as H
    rows = [
        {"round_index": 1, "posture": "explore", "directive": None,
         "gap_targeted": None, "rationale": "", "gaps_opened": [], "gaps_closed": [],
         "v1_directive": "complete", "v1_reason": "", "v1_maps_to": None,
         "shadow_verdict": "diverge", "applied_directive": "extend",
         "v2_applied": True, "prompt_mismatch": "gathering round receives...",
         "tool_called": None, "round_duration_s": None, "overran": False,
         "decision_inputs": None},
        {"round_index": 2, "posture": "alternatives", "directive": None,
         "gap_targeted": None, "rationale": "", "gaps_opened": [], "gaps_closed": [],
         "v1_directive": "complete", "v1_reason": "", "v1_maps_to": None,
         "shadow_verdict": "diverge", "applied_directive": "complete",
         "v2_applied": True, "prompt_mismatch": "routes decided, never generated",
         "tool_called": None, "round_duration_s": None, "overran": False,
         "decision_inputs": None},
        {"round_index": 3, "posture": "communicate", "directive": None,
         "gap_targeted": None, "rationale": "", "gaps_opened": [], "gaps_closed": [],
         "v1_directive": "complete", "v1_reason": "", "v1_maps_to": None,
         "shadow_verdict": "agree", "applied_directive": "complete",
         "v2_applied": True, "prompt_mismatch": None,
         "tool_called": None, "round_duration_s": None, "overran": False,
         "decision_inputs": None},
    ]
    orig = H._q
    H._q = lambda sql, p=None: rows
    try:
        trace = H._trace("cid", "v2")
    finally:
        H._q = orig
    assert [t["mismatch_kind"] for t in trace] == \
        ["mis_prompted", "not_generated", None]
    # the NAME is never the prose
    assert trace[1]["posture"] == "alternatives"


def test_v1_rows_carry_no_v2_only_fields():
    """A v1 row has no posture, no applied directive and no mismatch — absent,
    not null. The FE keys on presence, so a null would read as 'v2 ran and
    matched' on an arm where v2 never ran at all."""
    import app.api.ab_harness as H
    rows = [{"round_index": 1, "posture": None, "directive": None,
             "gap_targeted": None, "rationale": None, "gaps_opened": [],
             "gaps_closed": [], "v1_directive": "search", "v1_reason": "r",
             "v1_maps_to": None, "shadow_verdict": None,
             "applied_directive": None, "v2_applied": False,
             "prompt_mismatch": None, "tool_called": "rag",
             "round_duration_s": 6.0, "overran": False,
             "decision_inputs": None}]
    orig = H._q
    H._q = lambda sql, p=None: rows
    try:
        t = H._trace("cid", "v1")[0]
    finally:
        H._q = orig
    for f in ("applied_directive", "v2_applied", "prompt_mismatch", "mismatch_kind"):
        assert f not in t, f


def test_each_arm_names_the_turn_it_ran():
    """Without correlation_id nothing can link a comparison back to its own
    telemetry — the decision trace, the attestation, the logs. It was absent,
    and reading my own traces meant joining ab_run_questions by hand."""
    src = pathlib.Path("app/api/ab_harness.py").read_text()
    assert '"correlation_id": cid,' in src
