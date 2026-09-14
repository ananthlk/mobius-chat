"""react's feedback on last round's evidence, for the next corpus call.

Ananth, 2026-09-14: "you just need to pass feedback and the selected facts and
rejected facts.. do not solve just for this uqery". I had asked Retriever for a
`react_gap_sentences` field -- shaped around the one question in front of me,
which works for that question and quietly fails the next twenty.

IDS AND TEXT ONLY. Retriever's call: a chunk's TAGS are exactly what a retag
changes out from under it (measured: billing_codes.general 6 -> 26 in the pool,
coverage 74% -> 95%, between two probes an hour apart). They issued the id;
they hold the live row. No code, tag or classification leaves this process.
"""
import types

import app.pipeline.react_loop as RL


def _chunk(cid):
    return {"chunk_id": cid, "document_name": "D", "tags": {"a.b": 1}}


def _ctx(*, kept=(), served=(), gaps=(), preload_round=1, arm="v2"):
    c = types.SimpleNamespace(orchestrator_version=arm, correlation_id="cid-1")
    c._v2_kept_chunks = [_chunk(x) for x in kept]
    c._v2_preload_round = preload_round
    c._v2_preloaded = [{"tool": "rag", "rendered": [_chunk(x) for x in served]}]
    c._v2_last_contract = types.SimpleNamespace(
        gaps=tuple(types.SimpleNamespace(text=g, status="open") for g in gaps))
    return c


def test_not_useful_is_DERIVED_as_served_minus_cited():
    """react is asked for `kept` only -- one side that must be reliable rather
    than two. served-minus-cited is the complement, and the STRONGER signal:
    react's own rejection list omits whatever it forgot to mention."""
    fb = RL._v2_react_feedback(_ctx(served=("a", "b", "c"), kept=("b",)), 2)
    assert fb["kept_chunk_ids"] == ["b"]
    assert sorted(fb["not_useful_chunk_ids"]) == ["a", "c"]


def test_only_ids_and_text_cross_the_boundary():
    """No tag, no document_name, no classification -- those go stale under a
    retag and the consumer holds the live row anyway."""
    fb = RL._v2_react_feedback(_ctx(served=("a",), kept=("a",), gaps=("g",)), 2)
    blob = repr(fb)
    assert "tags" not in blob and "document_name" not in blob, fb
    assert set(fb) <= {"round", "kept_chunk_ids", "not_useful_chunk_ids", "gaps"}


def test_open_gap_sentences_travel_closed_ones_do_not():
    ctx = _ctx(served=("a",), kept=("a",))
    ctx._v2_last_contract = types.SimpleNamespace(gaps=(
        types.SimpleNamespace(text="still open", status="open"),
        types.SimpleNamespace(text="answered", status="closed")))
    fb = RL._v2_react_feedback(ctx, 2)
    assert fb["gaps"] == ["still open"]


def test_a_stale_served_list_contributes_NOTHING():
    """The numbered payload belongs to one round. Asking for round 5's feedback
    when the list is round 1's must not derive ids from the wrong list --
    every one would be wrong, and silently, because they are all real ids."""
    ctx = _ctx(served=("a", "b"), kept=("a",), preload_round=1)
    fb = RL._v2_react_feedback(ctx, 9)          # served list is for round 1
    assert not (fb or {}).get("not_useful_chunk_ids")


def test_nothing_to_say_is_None_not_an_empty_object():
    """Round 1 has no prior round. "nothing to report" and "no feedback
    channel" are different facts and the consumer must tell them apart."""
    assert RL._v2_react_feedback(_ctx(), 1) is None
    assert RL._v2_react_feedback(types.SimpleNamespace(), 2) is None


def test_the_wire_is_v2_only_and_corpus_only():
    """v1 is the control arm; moving its request body makes the A/B measure
    this instead of the orchestrator."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(RL))
    guarded = [n for n in ast.walk(tree)
               if isinstance(n, ast.If)
               and any(isinstance(c, ast.Constant) and c.value == "react_feedback"
                       for c in ast.walk(n))
               and any(isinstance(c, ast.Constant) and c.value == "orchestrator_version"
                       for c in ast.walk(n.test))
               and any(isinstance(c, ast.Constant) and c.value == "rag"
                       for c in ast.walk(n.test))]
    assert guarded, "the feedback wire is not gated on v2 AND on a corpus tool"


def test_no_kept_means_NO_not_useful_is_derived():
    """🔴 CAUGHT ON THE FIRST REAL TURN THROUGH THE LIVE WIRE.

        [v2.feedback] cid=29a1977c round=2 -> rag: kept=0 not_useful=11 gaps=3

    react returned no ordinals, so served-minus-kept became ALL ELEVEN served
    chunks -- and the consumer hard-excludes on this field. One missing
    self-report would have permanently removed every passage the turn saw.

    "kept nothing" and "did not say what it kept" are different facts and the
    subtraction cannot distinguish them. The dangerous reading must not be the
    one reachable by silence.
    """
    fb = RL._v2_react_feedback(_ctx(served=("a", "b", "c"), kept=(), gaps=("g",)), 2)
    assert fb is not None, "gaps should still travel"
    assert "not_useful_chunk_ids" not in fb, fb
    assert fb["gaps"] == ["g"]


def test_kept_present_still_derives_normally():
    fb = RL._v2_react_feedback(_ctx(served=("a", "b", "c"), kept=("b",)), 2)
    assert sorted(fb["not_useful_chunk_ids"]) == ["a", "c"]


# ── the address space is PER ROUND, and mid-turn rounds have one ────────────

def test_a_rounds_own_rendered_list_wins_over_preloads():
    """Measured, cid 29a1977c: the numbered list existed only for round 1
    (preload's), while react's evidence grew 22,996 -> 210,700 chars by round
    7. `kept` was asked over 23K of scoping material and never over the 210K
    react reasons from. kept=[] was react having nothing worth citing yet."""
    ctx = _ctx(served=("p1", "p2"), preload_round=1)
    ctx._v2_rendered_by_round = {4: [_chunk("m1"), _chunk("m2"), _chunk("m3")]}
    got = [c["chunk_id"] for c in RL._v2_kept_order(ctx, 4)]
    assert got == ["m1", "m2", "m3"], got
    # round 1 still resolves against preload's list
    assert [c["chunk_id"] for c in RL._v2_kept_order(ctx, 1)] == ["p1", "p2"]
    # a round that rendered nothing still resolves to nothing, not to a
    # neighbour's list -- an index means nothing without the list it indexes
    assert RL._v2_kept_order(ctx, 5) == []


def test_the_capture_is_inside_run_once_not_at_the_call_sites():
    """_run_once is called twice (attempt + retry). Capturing at the call
    sites means a third path added later silently skips it -- which is how
    this feature died four times: correct code on a path not taken."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(RL))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_once":
            calls = [n for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "_capture_rendered"]
            assert calls, "_run_once does not capture the render order"
            return
    raise AssertionError("_run_once not found")
