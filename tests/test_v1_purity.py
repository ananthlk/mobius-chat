"""v1 MUST STAY PURE. Ananth, 2026-09-12:

    "make sure v2 does not contaminate v1 in anyways.. v1 should stay pure for
     a/b compares.. including prompts and everything"

The A/B fork only measures ONE change if v1 behaves exactly as it did before v2
existed. A shared-path edit made for v2 reasons moves both arms, and then every
number after it is measuring two changes at once — which is worse than no
measurement, because it looks like one.

I ALREADY SHIPPED ONE. The react output ceiling was raised 1400 -> 8000 in
_call_llm_json, which serves BOTH arms. v2 needs the headroom (facts[] plus
thinking tokens); v1 does not, and its truncation behaviour would have changed
underneath the comparison. These gates exist so the next one is caught here.
"""
import ast
import inspect


def _fn(mod, name):
    tree = ast.parse(inspect.getsource(mod))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


# ── the shared LLM entry point ──────────────────────────────────────────────

def test_react_prompts_knows_nothing_about_arms():
    """THE VIOLATION THAT PROMPTED THIS FILE, and the shape of its fix.

    My first fix was an arm check INSIDE _call_llm_json. That works and is
    still contamination of a different kind: v1's file now contains v2's
    number and v2's reasoning, and the next person tuning v1 has to understand
    v2 to touch it. Ananth: "create a prompts v2 so that it is clean too".

    The real fix is that v2 passes its ceiling EXPLICITLY and a floor never
    lowers a caller's ask — so this file needs no arm knowledge at all."""
    import app.pipeline.react.prompts as P
    fn_src = inspect.getsource(P._call_llm_json)
    assert "orchestrator_version" not in fn_src, (
        "react/prompts.py is branching on the arm — v2 logic in v1's file")
    consts = {n.value for n in ast.walk(_fn(P, "_call_llm_json"))
              if isinstance(n, ast.Constant) and isinstance(n.value, int)}
    assert 1400 in consts, "v1's measured ceiling is gone"
    assert 8000 not in consts, "v2's ceiling leaked into v1's file"


def test_v2_owns_its_ceiling_in_its_own_module():
    from app.pipeline.v2 import prompts as V2P
    assert V2P.ROUND_MAX_TOKENS == 8000

    class _V2:
        orchestrator_version = "v2"

    class _V1:
        orchestrator_version = "v1"
    assert V2P.round_max_tokens(_V2()) == 8000
    # None, not v1's number: a v2 module must never hand v1 a value it chose.
    assert V2P.round_max_tokens(_V1()) is None
    assert V2P.round_max_tokens(object()) is None


def test_v1_never_imports_a_v2_module_on_its_path():
    """round_max_tokens() returns None for v1, but calling it still runs v2
    code and imports a v2 module on the arm that is supposed to be untouched —
    and an import error there would break exactly what must not move."""
    import app.pipeline.react_loop as rl
    src = inspect.getsource(rl)
    i = src.index("_v2_round_tokens = None")
    block = src[i:i + 400]
    # THE PROPERTY: an arm check precedes the v2 import. Asserted by ORDER,
    # not by a literal — my first version pinned the exact call text and
    # missed because of a str() wrapper's extra paren, which is the
    # fingerprint-not-property mistake one more time.
    assert "orchestrator_version" in block and '== "v2"' in block
    assert (block.index("orchestrator_version")
            < block.index("import prompts as _v2pr")), \
        "the v2 module is imported before the arm is checked"


# ── every v2 entry point in the shared loop is arm-gated ────────────────────

V2_CALLS = ("_v2_integrate", "_v2pre.plan", "_v2c.parse", "_v2mem.remember",
            "_v2mem0.recall", "_v2pr.round_max_tokens")


def test_every_v2_hook_in_react_loop_is_behind_an_arm_check():
    """Not "there is an if somewhere" — the v2 call must be DOMINATED by a
    check on orchestrator_version or a MOBIUS_V2_* env, or it runs for v1."""
    import app.pipeline.react_loop as rl
    src = inspect.getsource(rl)
    lines = src.splitlines()
    ungated = []
    # A function whose FIRST statements check the arm guards itself; its call
    # sites need no enclosing if. _v2_integrate is written that way on purpose
    # -- the guard belongs with the thing being guarded, not repeated at every
    # caller. Collect those first.
    self_guarding = set()
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef):
            head = ast.dump(ast.Module(body=n.body[:3], type_ignores=[]))
            if "orchestrator_version" in head:
                self_guarding.add(n.name)

    for i, line in enumerate(lines):
        st = line.lstrip()
        if st.startswith("#") or st.startswith("def "):
            continue
        if not any(c in line for c in V2_CALLS):
            continue
        if any(f"{name}(" in line and name in self_guarding
               for name in self_guarding):
            continue
        # Walk outward by indentation looking for the guard.
        ind = len(line) - len(line.lstrip())
        guarded = False
        j = i - 1
        while j >= 0 and ind > 0:
            l = lines[j]
            li = len(l) - len(l.lstrip())
            if l.strip() and li < ind:
                if ("orchestrator_version" in l or "MOBIUS_V2" in l
                        or "_v2_" in l or "v2_statements_sent" in l):
                    guarded = True
                    break
                if l.strip().startswith(("def ", "class ")):
                    break
                ind = li
            j -= 1
        if not guarded:
            ungated.append(f"{i + 1}: {line.strip()[:70]}")
    assert not ungated, "v2 hooks reachable on the v1 path:\n" + "\n".join(ungated)


def test_the_gate_can_see_a_v2_call_at_all():
    """A gate that inspects nothing passes forever."""
    import app.pipeline.react_loop as rl
    src = inspect.getsource(rl)
    assert any(c in src for c in V2_CALLS)


# ── prompts: v1's text must not carry v2's asks ─────────────────────────────

def test_v2_response_shape_lives_in_the_v2_frame_not_the_shared_prompt():
    """§11 asks for facts[]/not_useful[]. It belongs in v2/frame.py, which only
    v2 renders. In react/prompts.py it would change what v1 is asked to
    return — the loudest possible contamination."""
    import app.pipeline.react.prompts as P
    shared = inspect.getsource(P)
    assert "ALSO RETURN" not in shared
    assert '"not_useful"' not in shared
    from app.pipeline.v2 import frame as F
    assert "ALSO RETURN" in inspect.getsource(F)


def test_the_v1_blind_retry_is_still_there():
    """v2 deletes the code-level blind retry; v1 KEEPS it. Removing it from
    both would have made the arms incomparable in the other direction —
    'v2 is better' when what changed was v1 getting worse."""
    import app.pipeline.react_loop as rl
    src = inspect.getsource(rl)
    assert "_v2_no_blind_retry" in src
    i = src.index("_low_confidence_call_number = 1")
    block = src[i:i + 3000]
    assert 'orchestrator_version", "v1") == "v2"' in block, (
        "the blind-retry removal is not arm-gated — v1 lost its retry")


# ── the shared tool dispatch keeps v1's defaults ────────────────────────────

def test_the_retrieval_budget_override_defaults_to_v1_behaviour():
    """_execute_tool honours a caller-supplied token_budget_for_retrieval.
    v1 supplies none, so it must fall through to the computed budget it always
    used."""
    import app.pipeline.react_loop as rl
    src = inspect.getsource(rl)
    i = src.index('"token_budget_for_retrieval": (')
    assert "compute_token_budget_for_retrieval(ctx)" in src[i:i + 400]
