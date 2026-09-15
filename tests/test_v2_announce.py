"""Every major step announces, and the announcements say WHY.

Ananth, 2026-09-15: "we want every major step emitting their think.. every
major decision (e.g. rag, tools, prompts, llm) explaining their decision.. we
need every module kind of announcing so that we know how this runs."

The failure these guard is not a crash. It is a step quietly ceasing to
announce — the producer-without-consumer shape, inverted: a trace that still
renders, still passes, and no longer says the thing a reader needs. Nothing
goes red when a step stops emitting, so the gate has to be explicit.
"""

import ast
import inspect

from app.pipeline.v2 import announce as A
from app.pipeline.v2 import loop as L


def _src():
    return inspect.getsource(L)


#: The steps a reader needs to follow a turn. Each maps to the decision it
#: explains. If a step is removed, this list is the thing that must be argued
#: with — not a silently thinner trace.
REQUIRED = ["tools_running", "tools_selected", "posture", "tool_call",
            "tool_result", "prompt_built", "model_replied", "dropped", "exited"]


def test_every_announced_step_is_actually_emitted_by_the_loop():
    tree = ast.parse(_src())
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name)
              and n.func.value.id == "_announce"}
    missing = [s for s in REQUIRED if s not in called]
    assert not missing, (
        f"declared but never emitted: {missing} — a step that exists and is "
        "never called is a trace that looks complete and is not")


def test_the_posture_decision_is_announced_from_inside_the_round_loop():
    """THE DEFECT THIS CAUGHT: announce.posture was emitted only on the branch
    that ENDS the turn, so the one decision this loop exists to make was
    invisible on every round that continued.

    Asserts placement, not existence — "branch exists" is not "branch reached",
    and a posture step sitting only on the exit path is exactly that."""
    tree = ast.parse(_src())
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "run_react_v2")
    loops = [n for n in ast.walk(fn) if isinstance(n, (ast.While, ast.For))]

    def is_posture(n):
        return (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "posture")

    in_loop_not_after_break = False
    for lp in loops:
        for n in ast.walk(lp):
            if is_posture(n):
                # must not be nested under a statement that breaks out
                enclosing_breaks = [b for b in ast.walk(lp)
                                    if isinstance(b, ast.If)
                                    and any(isinstance(x, ast.Break)
                                            for x in ast.walk(b))
                                    and n in ast.walk(b)]
                if not enclosing_breaks:
                    in_loop_not_after_break = True
    assert in_loop_not_after_break, (
        "the posture decision is announced only on paths that end the turn — "
        "every continuing round decides a posture invisibly")


def test_no_step_can_claim_a_thing_does_not_exist():
    """WORLD_CLAIMS is empty: no tool outcome licenses telling a person that
    something is not there. A trace saying "not found" teaches the reader — and
    anyone quoting the trace — a claim the evidence cannot support.

    DOCSTRINGS ARE EXCLUDED BY NODE IDENTITY, not by comparing text. My first
    version compared each string against the set of docstrings and still
    caught this very docstring, because a banned phrase inside a longer
    docstring is not equal to it. That is the same defect six times over: a
    gate matching PROSE rather than the program. Strip by identity, then the
    only strings left are ones the module can actually emit.
    """
    banned = ("not found", "no results", "does not exist", "nothing exists")
    tree = ast.parse(inspect.getsource(A))

    doc_nodes = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                          ast.ClassDef)):
            body = getattr(n, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc_nodes.add(id(body[0].value))

    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
            continue
        if id(n) in doc_nodes:
            continue
        for phrase in banned:
            assert phrase not in n.value.lower(), (
                f"line {n.lineno} emits {phrase!r} — no tool outcome licenses "
                "a claim about what does not exist")


def test_outcome_words_come_from_the_shared_contract():
    """The five-word vocabulary is shared with the formatter. A second copy
    spelled here drifts, and the drift is silent on both sides."""
    src = _src()
    assert "mobius_contracts.taxonomies" in src, (
        "_outcome_word must import the shared vocabulary, not spell it")


def test_a_step_headline_carries_a_number_or_a_name():
    """Ananth, 2026-09-12: "a top line but an expandable". A headline a reader
    must expand to evaluate is a label, not a headline."""
    s = A.tools_selected(offered=["rag", "x"], chosen=["rag"])
    assert "1 of 2" in s.headline

    s = A.exited(stopped_by="model_complete_no_gaps", exit_mode=None,
                 rounds=2, answer_chars=500)
    assert "2 round(s)" in s.headline and "model_complete_no_gaps" in s.headline


def test_prompt_provenance_says_whether_the_db_answered():
    """A turn silently running the in-code fallback is how a prompt change
    appears to have no effect."""
    good = A.prompt_built(round=1, posture="explore", system_chars=10,
                          user_chars=5,
                          source={"source": "v1_composition",
                                  "composition_id": 86,
                                  "posture_block": "applied"})
    assert "composition 86" in "\n".join(good.detail)

    fell_back = A.prompt_built(round=1, posture="explore", system_chars=10,
                               user_chars=5,
                               source={"source": "inline", "failed": True})
    assert "FELL BACK" in "\n".join(fell_back.detail)


# ── the rag floor ───────────────────────────────────────────────────────────

def test_the_rag_floor_is_on_the_OUTCOME_not_a_constant():
    """Ananth: "no we will always do rag". Tool Manifest: a constant must not
    overrule estimate's own arithmetic, which SUPPRESSES rag with a stated
    reason rather than merely failing to rank it.

    Both are right, and the resolution is that the floor reads RESULTS:

      measured 2026-09-15, canonical question —
        rag_needed = False, because "offered tools reach density 1.00 ...
        retrieval would add nothing"
        the two tools carrying that density: inputs_status = fillable
        the same two at execution: rejected, missing required ['payor']
        so the density was over tools that cannot run, and rag returned
        21 sources once it was allowed to.

    The gate: the suppression must be honoured until the plan has RUN and
    produced nothing. An unconditional insert is a constant overruling a peer;
    a pre-emptive skip is could-not-check treated as checked-false.
    """
    tree = ast.parse(_src())
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "run_react_v2")

    for n in ast.walk(fn):
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
            for v in n.values:
                if (isinstance(v, ast.List) and len(v.elts) == 1
                        and isinstance(v.elts[0], ast.Constant)
                        and v.elts[0].value == "rag"):
                    raise AssertionError(
                        f'line {n.lineno}: `or ["rag"]` fires only on an EMPTY '
                        "offer — a non-empty list of tools that cannot "
                        "retrieve slips straight past it")

    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_has_evidence"]
    assert calls, (
        "nothing calls _has_evidence — the rag floor is not reading whether "
        "the preload actually produced anything, so it is either a constant "
        "overruling Tool Manifest or absent")

    guarded = any(isinstance(n, ast.If) and any(c in ast.walk(n.test)
                                                for c in calls)
                  for n in ast.walk(fn))
    assert guarded, "_has_evidence is called but not used as the condition"


def test_has_evidence_counts_sources_or_text_and_nothing_else():
    """A tool can SUCCEED and return an empty body; a tool rejected before
    calling raises no error at all. "Did it run" and "did it succeed" both
    read those as fine, which is how a turn reaches round 1 with nothing."""
    assert not L._has_evidence(None)
    assert not L._has_evidence([])
    assert not L._has_evidence([{"tool": "x", "success": True, "result": ""}])
    assert not L._has_evidence([{"tool": "x", "success": True, "result": "  ",
                                 "sources": []}])
    assert L._has_evidence([{"tool": "x", "success": True, "result": "text"}])
    assert L._has_evidence([{"tool": "x", "success": False,
                             "sources": [{"document": "d"}]}])
