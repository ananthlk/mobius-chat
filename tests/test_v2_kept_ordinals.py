"""react addresses passages by ordinal; we resolve ordinals to real chunks.

2026-09-13, Retriever's design. Two shapes were rejected first: chunk_id (an
opaque uuid react must copy exactly -- a wrong one parses fine and points the
diff at real-but-unrelated evidence) and (document, page) (safe to elicit but
collapses one on-point section in a large manual against ten irrelevant ones).
A small integer read off a list in front of the model is the one identifier it
reproduces reliably, and the real id is resolved server-side.

THE INVARIANT UNDER ALL OF IT: the number react echoes and the list we resolve
it against must be the same list in the same order. These tests assert that
end to end -- render, then resolve -- not each half alone. Testing the halves
separately is exactly how a caller's rewiring survives a green suite.
"""
import app.pipeline.v2.contract as C
import app.pipeline.v2.frame as F
from app.pipeline.v2 import statements as ST
from app.pipeline.v2.posture import Posture
from app.pipeline.v2.preload import _render_chunk, fair_share


def _src(name, page, text, arm="a", cid=None):
    return {"document_name": name, "page_number": page, "text": text,
            "arm": arm, "chunk_id": cid or f"{name}-{page}"}


# ── render ──────────────────────────────────────────────────────────────────

def test_rendered_passages_are_numbered_from_one():
    assert _render_chunk(_src("Manual", 4, "body"), 1).startswith("[1] [Manual p4]")
    assert _render_chunk(_src("Manual", 4, "body"), 12).startswith("[12] [Manual p4]")


def test_unnumbered_render_keeps_the_old_shape():
    """n=None must not grow a prefix -- callers with no list to address into."""
    out = _render_chunk(_src("Manual", 4, "body"))
    assert out.startswith("[Manual p4]"), out


def test_provenance_survives_numbering():
    """The ordinal is an ADDRESS; the document/page is what makes a citation
    checkable. Adding the first must not cost the second."""
    out = _render_chunk(_src("Sunshine Provider Manual", 111, "x"), 3)
    assert "Sunshine Provider Manual" in out and "p111" in out


def test_fair_share_numbers_match_the_kept_order():
    """THE PAIRING. kept[i-1] must be the passage rendered as [i]."""
    srcs = [_src(f"D{i}", i, f"text-{i}") for i in range(1, 6)]
    text, kept, _ = fair_share(srcs, per_arm_tokens=10_000, total_tokens=99_000)
    assert kept, "fixture produced no kept passages"
    for i, s in enumerate(kept, 1):
        assert f"[{i}] [{s['document_name']} p{s['page_number']}]" in text


# ── parse ───────────────────────────────────────────────────────────────────

def test_ints_accepts_what_a_model_actually_emits():
    assert C._ints([2, "5", " 7 "]) == (2, 5, 7)
    assert C._ints(3) == (3,)
    assert C._ints([2, 2, 5]) == (2, 5), "duplicates collapse"


def test_ints_refuses_non_indices():
    # 1-based: 0 is not an off-by-one to forgive, it means counting elsewhere.
    assert C._ints([0, -1, "x", None, {}]) == ()
    # bool is an int subclass and must not become index 1.
    assert C._ints([True, False]) == ()


def test_parse_reads_kept_from_the_contract_name():
    assert C.parse({"kept": [2, 5]}).kept_indices == (2, 5)
    assert C.parse({"kept_indices": [3]}).kept_indices == (3,)
    assert C.parse({}).kept_indices == ()


# ── resolve ─────────────────────────────────────────────────────────────────

def test_indices_resolve_against_the_rendered_order():
    srcs = [_src(f"D{i}", i, "t", cid=f"c{i}") for i in range(1, 6)]
    resp, chunks = C.with_kept_chunks(C.parse({"kept": [2, 5]}), srcs)
    assert [c["chunk_id"] for c in chunks] == ["c2", "c5"]
    assert resp.kept_indices == (2, 5)
    assert resp.kept_unresolved == ()


def test_an_index_react_invented_is_recorded_not_dropped():
    """A model addressing a list it cannot see is a PROMPT defect. Silently
    discarding the evidence of it is how that defect survives."""
    srcs = [_src("D1", 1, "t")]
    resp, chunks = C.with_kept_chunks(C.parse({"kept": [1, 99]}), srcs)
    assert resp.kept_indices == (1,)
    assert resp.kept_unresolved == (99,), "out-of-range index vanished"
    assert len(chunks) == 1


def test_no_indices_resolves_to_nothing_and_does_not_raise():
    resp, chunks = C.with_kept_chunks(C.parse({}), [])
    assert chunks == [] and resp.kept_indices == ()


# ── the ask is gated on a list existing ─────────────────────────────────────
#
# ASSERTED ON THE RENDERED PROMPT, not on the module source. A test that greps
# `inspect.getsource(F.render)` for "if numbered_passages:" passes on a gate
# that never executes, and passes on a comment -- which is the defect this
# repo has hit repeatedly. Build a real Ctx and read the text react gets.

def _frame(numbered):
    rs = ST.RoundState(round_index=1, open_gaps=(), gaps_open_history=(),
                       budget=95.0, next_round_cost_s=5.0, acting_cost_s=5.0,
                       validate_cost_s=5.0)
    c = ST.Ctx(state=rs, round_index=1, max_rounds=3, tier="thinking")
    txt, _ = F.render(c, Posture.NARROW, numbered_passages=numbered)
    return txt or ""


def test_kept_is_requested_when_passages_were_numbered():
    txt = _frame(4)
    assert '"kept"' in txt, "react is never asked which passages it used"
    # The range must name the ACTUAL count -- a hardcoded bound would point the
    # model at passages that do not exist on a shorter round.
    assert "[1] to [4]" in txt


def test_the_stated_range_tracks_the_real_count():
    assert "[1] to [9]" in _frame(9)
    assert "[1] to [4]" not in _frame(9)


def test_kept_is_NOT_requested_when_nothing_was_numbered():
    """Asking for indices with no list tells the model to address something it
    cannot see -- and a model asked for indices WILL produce some. They parse
    as valid ints and resolve against the wrong thing. This was the defect in
    the original proposal for this feature."""
    txt = _frame(0)
    assert '"kept"' not in txt, "kept asked for with zero numbered passages"


def test_the_key_count_in_the_preamble_matches_what_is_asked():
    """The preamble states how many keys are being added. If it says TWO while
    three are listed, the model drops one -- measured previously on this exact
    block, where an addendum framing lost facts[] entirely."""
    assert "THREE ADDITIONAL KEYS" in _frame(4)
    assert "TWO ADDITIONAL KEYS" in _frame(0)


# ── BOTH runners, because only one of them is the default ───────────────────
#
# 2026-09-13, live, cid 48ffdd23: numbering and `rendered` went into
# _preload_runner. Tool Manifest's executor (_preload_runner_toolreg) is the
# DEFAULT (react_loop:5419). So on every real turn passages were unnumbered,
# `rendered` was absent, numbered_passages was 0, and react was correctly never
# asked for "kept". The gate worked and the feature was inert.
#
# A test of one runner passes whichever runner ships. This asserts the contract
# on EVERY function that feeds _v2_kept_order.

def test_every_preload_runner_returns_rendered():
    """Any runner whose result reaches _v2_kept_order must carry `rendered`."""
    import ast as _ast
    import inspect

    import app.pipeline.react_loop as RL

    src = inspect.getsource(RL)
    tree = _ast.parse(src)
    runners = [n for n in tree.body
               if isinstance(n, _ast.FunctionDef)
               and n.name.startswith("_preload_runner")]
    assert len(runners) >= 2, f"expected both runners, found {[r.name for r in runners]}"

    for fn in runners:
        # Every dict literal RETURNED from a success path must key `rendered`.
        returns = [n for n in _ast.walk(fn)
                   if isinstance(n, _ast.Return)
                   and isinstance(n.value, _ast.Dict)]
        oks = []
        for r in returns:
            keys = {k.value for k in r.value.keys
                    if isinstance(k, _ast.Constant)}
            # success returns are the ones carrying real evidence
            if "sources" in keys and "payload" in keys:
                vals = dict(zip([k.value for k in r.value.keys if isinstance(k, _ast.Constant)],
                                r.value.values))
                ok_v = vals.get("ok")
                if isinstance(ok_v, _ast.Constant) and ok_v.value is False:
                    continue      # refusal path: no evidence, nothing to number
                oks.append(keys)
        assert oks, f"{fn.name}: found no success return to check"
        for keys in oks:
            assert "rendered" in keys, (
                f"{fn.name} returns evidence without `rendered` — "
                "_v2_kept_order will be empty on this path and react will "
                "never be asked which passages it kept"
            )


def test_kept_order_reads_rendered_from_every_preloaded_entry():
    import app.pipeline.react_loop as RL

    class _C:
        _v2_preloaded = [
            {"tool": "rag", "rendered": [{"chunk_id": "a"}, {"chunk_id": "b"}]},
            {"tool": "other", "rendered": []},
            {"tool": "prose"},                       # no key at all
        ]
    assert [c["chunk_id"] for c in RL._v2_kept_order(_C())] == ["a", "b"]
    assert RL._v2_kept_order(type("X", (), {"_v2_preloaded": []})()) == []


# ── the whole chain, because it broke in three separate places ──────────────
#
# runner -> preload.execute() -> ctx._v2_preloaded -> _v2_kept_order() ->
# numbered_passages -> the ask.
#
# Tonight this feature died THREE times, each silently: instrumented on the
# non-default runner; a log that only fired on success; and execute()
# rebuilding its result dict field-by-field and dropping `rendered`. Every
# unit on the path passed each time. Only the chain shows it.

def test_execute_carries_rendered_from_the_runner_to_the_caller():
    """execute() rebuilds its dict field-by-field -- a new field is dropped
    unless explicitly carried. It has already lost a 141k payload this way."""
    from app.pipeline.v2 import preload as P

    marker = [{"chunk_id": "c1"}, {"chunk_id": "c2"}]

    def runner(tool, inputs):
        return {"ok": True, "summary": "s", "payload": "[1] [D p1]\ntext",
                "sources": [{"chunk_id": "c1"}], "asked": "q",
                "rendered": marker}

    plan = P.plan(["rag"], inputs={"rag": {"query": "q"}})
    out = P.execute(plan, runner, question="q")
    rag = [r for r in out if r.get("tool") == "rag"]
    assert rag, f"rag did not execute: {[r.get('tool') for r in out]}"
    assert rag[0].get("rendered") == marker, (
        "execute() dropped `rendered`; _v2_kept_order() will be empty and "
        "react is never asked which passages it kept"
    )


def test_the_full_chain_produces_a_nonzero_numbered_count():
    """End to end: a runner returning `rendered` must make numbered_passages
    non-zero at the frame. Each half passed alone while the chain was broken."""
    import app.pipeline.react_loop as RL
    from app.pipeline.v2 import preload as P

    def runner(tool, inputs):
        return {"ok": True, "summary": "s", "payload": "p",
                "sources": [], "asked": "q",
                "rendered": [{"chunk_id": f"c{i}"} for i in range(3)]}

    plan = P.plan(["rag"], inputs={"rag": {"query": "q"}})
    ctx = type("C", (), {"_v2_preloaded": P.execute(plan, runner, question="q")})()
    assert len(RL._v2_kept_order(ctx)) == 3, (
        "the chain runner->execute->ctx->_v2_kept_order lost the render order"
    )
