"""SIZE decides the move; KIND does not. And the vocabulary is READ, not copied.

Deep Research measured kind twice and it failed twice: `doc_type` NULL on
13,642 of ~17,000, `d_tags` at 99.9% coverage but `health_care_services`
dominant on 12,274 of 16,976 -- a near-constant separates nothing.

The words live in THEIR module and are published to
research.size_class_vocabulary. Copying them here would be the cross-repo
form of the drift plan_shape.py prevents in-repo: two copies, diverging
silently, with no gate able to see both.
"""
import ast
import pathlib

from app.pipeline.v2 import posture_prompts as P
from app.pipeline.v2 import size_class as S


def flat(t: str) -> str:
    return " ".join((t or "").split())


class TestFrameAsksForTheAxisThatPays:
    def test_frame_asks_for_an_expected_size_class(self):
        assert "expected_size_class" in flat(P.FRAME)

    def test_frame_says_size_decides_not_kind(self):
        """The correction itself. Without this line the model is told to name
        a class and never told why it outranks the kind it just wrote."""
        f = flat(P.FRAME)
        assert "Size, not kind, is what decides" in f


class TestTheWordsAreNotCopiedIntoThisRepo:
    def test_no_class_text_is_hardcoded_in_our_prompt_module(self):
        """The moves are Deep Research's wording, published to a table. A
        literal here is a second copy that no gate on either side can see."""
        src = pathlib.Path(P.__file__).read_text()
        for phrase in ("READ IT WHOLE", "NAME THE SUBJECT AND READ THE SECTION",
                       "RETRIEVAL IS THE CEILING"):
            assert phrase not in src, (
                f"{phrase!r} is hardcoded in posture_prompts.py — it must be "
                f"read from research.size_class_vocabulary")

    def test_the_reader_holds_no_fallback_copy_of_the_words(self):
        """The tempting shape: a hardcoded default so the block still renders
        when the table is unreachable. That default IS the second copy, and it
        is the worst kind -- it only appears when nobody is looking, so it
        drifts unobserved and looks correct on every good day.

        (My first version of this test counted the table NAME in the source
        and failed on the docstring. A test that reads prose instead of
        program is the defect I have now shipped three times.)
        """
        src = pathlib.Path(S.__file__).read_text()
        tree = ast.parse(src)
        # String CONSTANTS in executable positions only -- docstrings excluded.
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                                 ast.AsyncFunctionDef)):
                d = ast.get_docstring(node, clean=False)
                if d:
                    docstrings.add(d)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in docstrings:
                    continue
                for phrase in ("READ IT WHOLE", "NAME THE SUBJECT",
                               "RETRIEVAL IS THE CEILING"):
                    assert phrase not in node.value, (
                        f"{phrase!r} is a hardcoded fallback in the reader")


class TestTheExpectedFaceIsAPriorNotAMeasurement:
    def test_unmeasured_is_excluded_from_the_frame_face(self):
        """`unmeasured` is a fact about OUR corpus (holds table text, not yet
        sized), not something answerable from general knowledge about a genre.
        FRAME runs before anything is fetched."""
        assert "unmeasured" not in S.EXPECTED_CLASSES

    def test_the_three_expected_classes_are_the_ladder_in_order(self):
        assert S.EXPECTED_CLASSES == ("read_whole", "section_read",
                                      "retrieval_only")


class TestItFailsOpenAndOnAShortLeash:
    def test_an_unreadable_vocabulary_yields_an_empty_block_not_an_error(self,
                                                                        monkeypatch):
        """FRAME without the size block is FRAME as it was yesterday. FRAME
        behind a hanging socket is a turn nobody gets."""
        monkeypatch.setattr(S, "_CACHE", None)
        monkeypatch.setattr(S, "_rows", lambda: (_ for _ in ()).throw(
            RuntimeError("db down")))
        assert S.expected_block() == ""

    def test_the_connect_is_bounded(self):
        """I reported Tool Manifest's estimate() today for an unbounded
        psycopg2.connect on the per-turn path -- it blocked 45-75s and was
        charged to the turn budget. An unbounded connect here would be the
        same defect with my name on it."""
        src = pathlib.Path(S.__file__).read_text()
        tree = ast.parse(src)
        connects = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "connect"
        ]
        assert connects, "no connect call found — has the reader changed shape?"
        for c in connects:
            assert any(k.arg == "connect_timeout" for k in c.keywords), (
                "psycopg2.connect without connect_timeout on the prompt path")

    def test_a_rendered_block_marks_itself_as_a_prior(self, monkeypatch):
        monkeypatch.setattr(S, "_CACHE", None)
        monkeypatch.setattr(S, "_rows", lambda: [
            {"name": "read_whole", "rule": "at most 30,000 characters",
             "move": "READ IT WHOLE", "why": "one read serves it",
             "kinds": ["a rule"]}])
        b = S.expected_block()
        assert "PRIOR, NOT A MEASUREMENT" in flat(b)
        assert "have not checked" in flat(b)

    def test_kinds_are_genres_and_never_named_documents(self, monkeypatch):
        """A specific filename or heading would be a claim about a document
        nobody opened -- which cost Deep Research a run today."""
        monkeypatch.setattr(S, "_CACHE", None)
        monkeypatch.setattr(S, "_rows", lambda: [
            {"name": "read_whole", "rule": "r", "move": "M", "why": "w",
             "kinds": ["a rule", "a coverage policy"]}])
        b = flat(S.expected_block())
        assert ".pdf" not in b
        assert "§" not in b


class TestReadWholeIsOfferedAsAMove:
    """Live turn 54c3f84f asked "Open the Sunshine Provider Manual..." and the
    loop ran rag, answered from chunks, and never called fetch_document.

    Every part worked: estimate() offers the tool, its inputs are `fillable`,
    react can dispatch it, and a small-enough document returns as an inline
    attachment the next round reads whole. Nothing ever asked. The prompt had
    no rung for it, so the loop took the 16% path on a question that NAMED
    the document.
    """

    # 🔴 THESE RENDER THE BLOCK, THEY DO NOT READ P.EXPLORE.
    #
    # EXPLORE is built at import time and the block is empty when the
    # vocabulary table is unreachable -- which is the fail-open path, and is
    # the normal state in a test runner. Asserting on the module constant
    # made these tests pass or fail on whether a DATABASE was up, which is
    # the exact fragility that cost me an hour on the estimate() hang today.
    def test_the_block_names_the_tool_that_reads_a_document(self, monkeypatch):
        monkeypatch.setattr(S, "_rows", lambda: [
            {"name": "read_whole", "rule": "at most 30,000 characters",
             "move": "READ IT WHOLE", "why": "one read serves it",
             "kinds": []}])
        b = flat(S.escalation_block())
        assert "fetch_document" in b, (
            "the block never names the tool that performs the highest-settling "
            "move, so the model cannot plan it")

    def test_the_block_says_a_named_document_is_one_you_have_not_read(self,
                                                                      monkeypatch):
        monkeypatch.setattr(S, "_rows", lambda: [
            {"name": "read_whole", "rule": "r", "move": "M", "why": "w",
             "kinds": []}])
        b = flat(S.escalation_block())
        assert "NAMES A DOCUMENT" in b
        assert "ranked" in b and "out of it" in b

    def test_explore_carries_the_block_when_the_vocabulary_is_readable(self):
        """The wiring, asserted on the SOURCE rather than on a rendered
        constant that depends on a live database."""
        import ast
        src = pathlib.Path(P.__file__).read_text()
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "escalation_block"]
        assert calls, "EXPLORE never calls escalation_block()"

    def test_only_classes_with_a_real_tool_are_offered(self):
        """`section_read` has no tool surface on our side -- a document id AND
        a section, not fillable from a bare question. Rendering it would teach
        the model to plan a move nobody can execute, which is exactly what
        Deep Research's `as_tool` flag exists to prevent."""
        assert "section_read" not in S.TOOL_FOR_CLASS
        assert set(S.TOOL_FOR_CLASS) <= set(S.EXPECTED_CLASSES)

    def test_every_offered_class_names_a_tool_that_is_not_none(self):
        for name, tool in S.TOOL_FOR_CLASS.items():
            assert tool, f"{name} is offered with no tool"

    def test_the_ladder_fails_open_like_the_rest(self, monkeypatch):
        monkeypatch.setattr(S, "_rows", lambda: (_ for _ in ()).throw(
            RuntimeError("db down")))
        assert S.escalation_block() == ""
