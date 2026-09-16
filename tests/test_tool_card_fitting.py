"""A tool with a UX card gets that card — whichever route ran it.

Ananth, looking at the appeals admin next to the chat answer, 2026-09-16:
"here is what the tool has and here is what is displayed.. why are we
condensing it so much especially when all of this is available".

THE TOOL HAD: COB.R001 "Medicaid Payor of Last Resort" — a rule id, CARC 22
and 23 codes, a rule STATEMENT and a separately-authored "our argument", a
provenance tier and a review state. Four such rules.

THE READER GOT: four bold-led bullets. The ids, the statement/argument
distinction, the codes and the provenance were discarded by a model asked to
summarise a structure it had been handed whole.

WHY. Every appeals card in this product reaches the screen through a
`section_hint`, and only CHAT's branches emit one. The same tool served by Tool
Manifest's MCP route returns generic evidence, so the card became a paraphrase
depending on which code path executed the tool.

A card is a property of the TOOL, not of the route. The declaration belongs in
Tool Manifest's registry (docs/v2-loop/tool-declares-its-card.md); the map here
is temporary and exists to exercise the contract on real traffic first.
"""

import json

import app.pipeline.react_loop as R

RULES = [
    {"rule_id": "COB.R001", "title": "Medicaid Payor of Last Resort",
     "rule_statement": "A claim is submitted to Medicaid for a beneficiary who "
                       "has other credible health care coverage.",
     "appeal_argument": "The member had no other credible health care coverage.",
     "carc_codes": [22, 23]},
    {"rule_id": "COB.R003", "title": "Florida Medicaid Third-Party Liability",
     "rule_statement": "A claim is submitted where another entity may be "
                       "financially responsible.",
     "appeal_argument": "No other entity was financially responsible."},
]


def _hint(tool, payload, success=True):
    return R._card_hint_for_tool(
        {"tool": tool, "success": success, "result": json.dumps(payload)})


def test_structured_rules_become_the_appeals_card_not_bullets():
    h = _hint("appeals_lookup_rules", {"rules": RULES, "carc": 22})
    assert h and h["section_format"] == "appeals_rules"
    # THE POINT: the rules travel WHOLE. Ids, statements and arguments are the
    # fields the card renders separately and the paraphrase destroyed.
    got = h["data"]["rules"]
    assert [r["rule_id"] for r in got] == ["COB.R001", "COB.R003"]
    assert got[0]["rule_statement"] != got[0]["appeal_argument"]


def test_find_carc_nests_its_rules_and_is_flattened():
    """appeals_find_carc returns matches[].rules[]; one card shape serves both
    tools rather than two near-identical readers."""
    h = _hint("appeals_find_carc", {"matches": [{"rules": RULES}], "carc": 22})
    assert h and len(h["data"]["rules"]) == 2


def test_a_card_that_would_come_out_empty_falls_through_to_prose():
    """Worse than prose: it looks authoritative and says nothing."""
    assert _hint("appeals_lookup_rules", {"rules": []}) is None
    assert _hint("appeals_lookup_rules", {"carc": 22}) is None
    assert _hint("appeals_get_playbook", {"payor": "Sunshine Health"}) is None


def test_the_playbook_card_needs_one_filing_critical_field():
    """requires_any, measured: FL Medicaid rows carry submission_method with a
    null deadline — 72 of 141 rows. Requiring both refuses half the library."""
    assert _hint("appeals_get_playbook",
                 {"payor": "FL Medicaid", "submission_method": "portal"})
    assert _hint("appeals_get_playbook",
                 {"payor": "Sunshine Health", "deadline_appeal_days": 90})


def test_an_undeclared_tool_is_untouched():
    """No card declared means format as prose, exactly as today."""
    assert _hint("rag", {"chunks": ["text"]}) is None
    assert _hint("search_corpus", {"rules": RULES}) is None


def test_a_failed_call_never_becomes_a_card():
    assert _hint("appeals_lookup_rules", {"rules": RULES}, success=False) is None


def test_the_fitter_is_reached_from_the_hint_collection():
    """The producer-with-no-producer check. A fitter nothing calls is a card
    nothing renders — three times today I have wired work into a path that does
    not run."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(R))
    fn = next(f for f in ast.walk(tree)
              if isinstance(f, ast.FunctionDef) and f.name == "_finalize_response")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_card_hint_for_tool" in called, (
        "nothing fits tool output to its declared card — every appeals result "
        "stays a paraphrase")
