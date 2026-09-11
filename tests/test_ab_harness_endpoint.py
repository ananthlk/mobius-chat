"""The A/B harness endpoint — shape assertions against the FE render contract.

These assert the CONTRACT, not the implementation: the FE builds against this
shape and a silent change to it breaks a page in another seat's repo.
"""

import inspect

from app.api import ab_harness as ab


def test_the_four_routes_the_contract_names_exist():
    paths = {f"{list(r.methods)[0]} {r.path}" for r in ab.router.routes}
    assert paths == {
        "POST /ab/runs", "GET /ab/runs",
        "GET /ab/runs/{run_id}", "GET /ab/runs/{run_id}/q/{qid}",
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
