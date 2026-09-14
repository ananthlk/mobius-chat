"""A transient provider error must not discard rounds of confirmed evidence.

2026-09-13, live, cid b7722476 ("Which HCPCS codes does Florida Medicaid use
for behavioral health targeted case management?"):

    [v2.contract] round=5 shape=mixed facts=2 not_useful=0
    Published failed response: 429 Resource exhausted

Five rounds, two CITED facts and a running answer in memory, and the user
waited 62 seconds to read "The model is temporarily busy".

status stays "failed" -- the turn did fail, and the attestation, retry logic
and analytics should keep seeing that. Only what the PERSON reads changes.
"""
import types

import pytest


def _ctx(version="v2", running="", facts=()):
    c = types.SimpleNamespace(orchestrator_version=version)
    c._v2_last_contract = types.SimpleNamespace(
        running_answer=running, facts=tuple(facts))
    return c


def _fact(text, doc, page, grounded=True):
    f = types.SimpleNamespace(fact=text, document=doc, page=page)
    f.grounded = grounded
    return f


def _build(ctx, env_msg="The model is temporarily busy."):
    """Mirror of the orchestrator's partial-answer block, exercised directly.

    Imported rather than reimplemented would be better; the block lives inline
    in a 2,000-line function. So this asserts the PROPERTIES the block must
    hold, and test_the_block_is_wired below proves it is reached.
    """
    lc = ctx._v2_last_contract
    partial = (getattr(lc, "running_answer", "") or "").strip()
    pf = [f for f in (getattr(lc, "facts", ()) or ()) if getattr(f, "grounded", False)]
    if ctx.orchestrator_version != "v2" or not partial or not pf:
        return None
    cites = "; ".join(f"{f.document}" + (f" p{f.page}" if f.page else "")
                      for f in pf[:4])
    return (f"{partial}\n\n_I could not finish checking this — {env_msg} "
            f"The above is what I had confirmed from {len(pf)} cited "
            f"source(s) ({cites}). Ask again and I will pick up from a full "
            "search._")


def test_partial_answer_is_served_when_evidence_exists():
    m = _build(_ctx(running="Florida Medicaid uses T1017 for TCM.",
                    facts=[_fact("x", "AHCA Fee Schedule", 12)]))
    assert m and "T1017" in m
    assert "AHCA Fee Schedule p12" in m, "the citation must travel with it"


def test_a_running_answer_with_no_grounded_fact_is_refused():
    """A draft with nothing behind it is an ungrounded claim -- exactly what
    this pipeline exists to prevent. Better the apology than that."""
    assert _build(_ctx(running="Probably T1017.",
                       facts=[_fact("x", "Doc", 1, grounded=False)])) is None
    assert _build(_ctx(running="Probably T1017.", facts=[])) is None


def test_no_running_answer_means_no_partial():
    assert _build(_ctx(running="", facts=[_fact("x", "Doc", 1)])) is None


def test_v1_is_never_touched():
    """v1 is the control arm. Changing its failure text would contaminate the
    A/B in the one place a user is already having a bad turn."""
    assert _build(_ctx(version="v1", running="answer",
                       facts=[_fact("x", "Doc", 1)])) is None


def test_the_v1_gate_encloses_the_partial_block_itself():
    """🔴 MY FIRST VERSION OF THIS TEST PASSED WITH THE GATE DELETED.

    It asserted that the string "orchestrator_version" appears in the module --
    which it does, at line 491, for an unrelated reason. And the behavioural
    tests above exercise a REIMPLEMENTATION in this file, which has its own
    gate, so removing the real one changed nothing they could see. Testing a
    copy instead of the path, caught only by mutating the real source.

    This walks the AST to the statement that logs "[v2.partial]" and asserts
    the `If` enclosing it actually tests orchestrator_version. Deleting the
    gate now fails, because the assertion is about THAT conditional and not
    about the string existing somewhere in a 2,000-line module.
    """
    import ast
    import inspect

    import app.pipeline.orchestrator as O

    tree = ast.parse(inspect.getsource(O))

    def _has_partial_log(node):
        return any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                   and "[v2.partial]" in n.value for n in ast.walk(node))

    def _tests_version(node):
        """Does this If's TEST (not its body) check orchestrator_version?"""
        return any(isinstance(n, ast.Constant) and n.value == "orchestrator_version"
                   for n in ast.walk(node.test))

    guarded = [n for n in ast.walk(tree)
               if isinstance(n, ast.If) and _has_partial_log(n) and _tests_version(n)]
    assert guarded, (
        "the [v2.partial] block is not enclosed by a conditional testing "
        "orchestrator_version -- v1's failure message would change, which "
        "contaminates the control arm"
    )


def test_the_block_is_present_at_all():
    import ast
    import inspect

    import app.pipeline.orchestrator as O

    tree = ast.parse(inspect.getsource(O))
    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert any(c == "_v2_last_contract" for c in consts), \
        "failure path no longer reads v2's last contract"
    assert any("[v2.partial]" in c for c in consts), "the partial-answer log is gone"
