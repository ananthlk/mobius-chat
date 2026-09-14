"""v2 answers shipped with zero sections; v1's shipped with bullets and tables.

Measured on 15 paired A/B questions, 2026-09-14:
    v1  avg 0.7 sections/answer  (bullets x8, table x3)
    v2  avg 0.0 sections         -- every answer one prose block

v1's integrator is an LLM that WRITES structure. v2 replaced it with a
deterministic formatter that can only PRESERVE structure the draft has, and
classify_envelope correctly refuses react's prose: "no confident structure;
prose is the honest rendering".

But v2 holds FACTS carrying their document and page. Those are structure we
were GIVEN, not inferred from prose -- so rendering them is faithful, not a
heuristic.
"""
import types

# noqa: F401 -- import order avoids a circular import, same as
# test_appeals_source_texts.py and test_detail_ready.py. app.stages.integrate
# cannot be the FIRST import: orchestrator imports run_integrate from it while
# it is still initialising. This is pre-existing and not introduced here.
from app.pipeline.context import PipelineContext  # noqa: F401
from app.stages.integrate import _sections_from_v2_facts


def _build(card, ctx):
    return _sections_from_v2_facts(card, ctx)


def _fact(text, doc="Manual", page=4, grounded=True):
    f = types.SimpleNamespace(fact=text, document=doc, page=page)
    f.grounded = grounded
    return f


def _ctx(*facts):
    return types.SimpleNamespace(
        _v2_last_contract=types.SimpleNamespace(facts=tuple(facts)))


def test_grounded_facts_become_a_cited_bullets_section():
    card = _build(
        {"sections": []},
        _ctx(_fact("Molina allows 365 days", "Molina Manual", 12),
             _fact("Sunshine allows 180 days", "Sunshine Manual", 7)))
    sec = card["sections"][0]
    assert sec["format"] == "bullets"
    assert "Molina Manual p12" in sec["bullets"][0]
    assert "Sunshine Manual p7" in sec["bullets"][1]


def test_ungrounded_facts_are_refused():
    """A fact with no document is the ungrounded claim v2 exists to keep out.
    Promoting it to a section gives it MORE prominence than the prose did."""
    card = _build(
        {"sections": []},
        _ctx(_fact("something true-sounding", grounded=False),
             _fact("also unsourced", grounded=False),
             _fact("third", grounded=False)))
    assert card["sections"] == []


def test_a_single_bullet_is_not_a_list():
    card = _build({"sections": []},
                                   _ctx(_fact("only one thing")))
    assert card["sections"] == []


def test_repeated_facts_collapse():
    """The same fact re-stated across rounds is one thing learned, not two."""
    card = _build(
        {"sections": []},
        _ctx(_fact("Molina allows 365 days"),
             _fact("molina   ALLOWS 365 days"),
             _fact("Sunshine allows 180 days", "S", 1)))
    assert len(card["sections"][0]["bullets"]) == 2


def test_an_existing_section_is_never_overridden():
    """A draft that DID carry clean structure keeps it -- the caller only
    reaches this when the formatter produced nothing, and this must not be the
    thing that changes that."""
    card = {"sections": [{"format": "table", "label": "kept"}]}
    out = _build(card, _ctx(_fact("a"), _fact("b")))
    assert out["sections"][0]["label"] == "kept"
    assert len(out["sections"]) == 2


def test_no_contract_at_all_is_survivable():
    assert _build({"sections": []},
                                   types.SimpleNamespace())["sections"] == []


def test_a_fact_with_no_page_still_cites_its_document():
    card = _build(
        {"sections": []},
        _ctx(_fact("x", "Doc A", None), _fact("y", "Doc B", None)))
    assert "[Doc A]" in card["sections"][0]["bullets"][0]


def test_the_builder_is_wired_into_the_v2_card_path():
    """🔴 MY MUTATION CHECK PASSED WITHOUT THIS.

    Removing the call from integrate.py left all seven tests above green,
    because they exercise the function directly. Testing the unit instead of
    the path -- the same defect that made the ordinal scheme inert for three
    deploys tonight.

    Asserts the call exists AND sits under the empty-sections guard, so a
    version that clobbers a formatter-produced card also fails.
    """
    import ast
    import inspect

    import app.stages.integrate as I

    tree = ast.parse(inspect.getsource(I))

    def _calls_builder(node):
        return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_sections_from_v2_facts"
                   for n in ast.walk(node))

    guarded = [n for n in ast.walk(tree)
               if isinstance(n, ast.If) and _calls_builder(n)
               and any(isinstance(c, ast.Constant) and c.value == "sections"
                       for c in ast.walk(n.test))]
    assert guarded, (
        "_sections_from_v2_facts is not called under a check on existing "
        "sections -- either it never runs, or it can overwrite structure the "
        "formatter legitimately produced"
    )
