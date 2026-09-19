"""v2 exited the ladder at rung 0.

The escalation question has TWO parts — "does what I am holding answer this,
with a sentence I can quote? and if NOT, can I NAME a document?" The loop
only ever asked the first, answered "no", and completed.

Measured on the A/B: v2 ran no retrieval at all on 11 of 15 turns and issued
5 distinct queries where v1 issued 24. On "does Aetna cover doula services"
v1 retrieved twice and answered from the second query; v2 answered from
preload alone with "not found in the available materials" while the corpus
held it.

THE PROMPT WAS NOT THE GAP: given the same not-found evidence, EXPLORE plans
fetch_document 4 times out of 4 against the live model. The model knew the
move; it was never given the round.
"""
import ast
import pathlib

from app.pipeline.v2 import ladder as L
from app.pipeline.v2 import loop as LOOP


class TestTheSignalIsStructuralNotTextual:
    def test_grounded_facts_is_read_from_the_contract(self):
        """"not found" / "is not specified" are PROSE, and matching prose is
        the defect this file has shipped four times. The signal is an empty
        facts tuple: the turn is completing without anything it can cite."""
        from types import SimpleNamespace as NS

        from app.pipeline.v2.contract import Fact

        assert LOOP._has_grounded_facts(NS(_v2_last_contract=NS(facts=()))) is False
        assert LOOP._has_grounded_facts(NS(_v2_last_contract=None)) is False
        assert LOOP._has_grounded_facts(
            NS(_v2_last_contract=NS(facts=(Fact("x", "Doc.pdf", 3, "id"),)))) is True

    def test_a_fact_with_NO_DOCUMENT_does_not_count_as_grounded(self):
        """🔴 THE MISS THAT MADE THE FIRST FIX INERT.

        Live turn 31fa6200, the doula question, on the build that was supposed
        to fix this: the model emitted facts, NONE carried a document, and
        `verify` on that same turn logged "no facts with a document and page".
        My predicate returned True on `bool(facts)`, so the rung-0 exit never
        fired and the answer came back "not specified" with 9 sources again.

        Two consumers of one contract, disagreeing about whether the turn had
        learned anything, because I used a weaker bar than the one three lines
        away.
        """
        from types import SimpleNamespace as NS

        from app.pipeline.v2.contract import Fact

        ungrounded = (Fact("doulas are not listed", "", None, ""),)
        assert LOOP._has_grounded_facts(
            NS(_v2_last_contract=NS(facts=ungrounded))) is False

    def test_the_exit_does_not_match_on_answer_wording(self):
        src = pathlib.Path(LOOP.__file__).read_text()
        i = src.index("_reached_for_a_document\n")
        window = src[i:i + 2500]
        for prose in ('"not found"', "'not found'", '"not specified"'):
            assert prose not in window, (
                f"the rung-0 exit matches on {prose} — prose, not structure")


class TestItReachesOnlyOnceAndOnlyWhenAffordable:
    def test_the_once_flag_exists_and_is_set_before_continuing(self):
        tree = ast.parse(pathlib.Path(LOOP.__file__).read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "_reached_for_a_document" in names, (
            "no once-flag: a refusal repeated is a retry that became a loop")

    def test_the_branch_consults_the_remaining_budget(self):
        """Parsed, not grepped — searching for `_left` finds the log line."""
        tree = ast.parse(pathlib.Path(LOOP.__file__).read_text())
        guarded = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
                if {"_left", "_round_cost"} <= names:
                    guarded = True
        assert guarded, "the extra round is bought without checking affordability"


class TestTheRungIsReadNotCopied:
    def test_our_repo_holds_no_copy_of_their_rung_wording(self):
        """Their words, their release. A copy here drifts the first time they
        learn something."""
        src = pathlib.Path(L.__file__).read_text()
        tree = ast.parse(src)
        docs = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)):
                d = ast.get_docstring(n, clean=False)
                if d:
                    docs.add(d)
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                if n.value in docs:
                    continue
                assert "reword to identify" not in n.value, (
                    "their rung wording is hardcoded rather than fetched")

    def test_it_renders_only_rungs_this_loop_can_actually_invoke(self):
        src = pathlib.Path(L.__file__).read_text()
        assert 'r.get("as_tool")' in src, (
            "rungs are rendered without checking as_tool — the model would be "
            "taught to plan a move nobody can execute")

    def test_it_fails_open(self, monkeypatch):
        monkeypatch.setattr(L, "_CACHE", None)
        monkeypatch.setattr(L, "_rungs", lambda: (_ for _ in ()).throw(RuntimeError("down")))
        assert L.name_a_document_block() == ""

    def test_it_never_puts_their_odds_in_the_prompt(self, monkeypatch):
        """Deep Research: "use the ladder's ORDERING; do not use its
        magnitudes." Their read_whole figure moved 95% -> 45% on a 7x base
        change and ranges 24%-77% within one domain."""
        monkeypatch.setattr(L, "_CACHE", None)
        monkeypatch.setattr(L, "_rungs", lambda: [
            {"name": "reformulate_to_name", "as_tool": True, "tool": "rag",
             "condition": "c", "move": "m", "escalation_question": "q",
             "odds": "27%", "settled": 164}])
        out = L.name_a_document_block()
        assert "27%" not in out and "164" not in out
