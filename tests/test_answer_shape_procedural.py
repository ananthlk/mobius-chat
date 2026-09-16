"""A procedural question asks for steps — and could not, for the life of the
feature.

Ananth, on live cards: "IT STILL FEELS BADLY FORMATTED" and "i also dont think
the deterministic module is getting to work no formatting in any of my replies".

THE FORMATTER WAS NEVER THE PROBLEM. Measured directly: hand the classifier the
same appeal answer written three ways and it returns three formats —
`*` bullets -> bullets, `1. 2. 3.` -> steps, `Label: value` -> stats. It reads
shape correctly every time.

The shape never varied because nothing ever asked for a different one:

  1. REACT_FORMAT_RULES_TEXT tells the model, on every answer, "follow with 2-4
     short bullet points". So the input to the classifier is always bullets.

  2. v2 HAS an answer_shape block built to fix exactly that, and its own
     comment says so — "a three-entity comparison is STRUCTURALLY unable to
     come back as one... the model having no option to choose". Its procedural
     branch was UNREACHABLE: `_PROCEDURAL` is written to match a QUESTION
     ("how do i", "how to") and `_is_procedural` applied it to `question.axis`,
     a noun phrase extracted for a table header that comes back "" for every
     procedural question measured.

A pattern built for one field, applied to another. Both sides individually
correct, the feature dead, and no test noticed because no test crossed the two.
"""

from app.responder.answer_template import (QuestionShape, suggest_from_question,
                                           suggest_template)


def _asks_for_steps(question: str) -> bool:
    s = suggest_from_question(question)
    return bool(s and "numbered list" in s)


def test_how_do_i_questions_ask_for_steps():
    """The live question that started this, and its siblings."""
    for q in ("how do i appeal a CARC 22 denial for sunshine health?",
              "how do i submit a corrected claim to Aetna?",
              "how can i verify a TPL record for the member?",
              "what steps must a provider take to request a peer-to-peer review?"):
        assert _asks_for_steps(q), f"not procedural: {q!r}"


def test_a_lookup_is_not_a_procedure():
    """The other direction matters as much: turning every question into steps
    would be the same defect wearing a different shape."""
    for q in ("what is the timely filing deadline for Sunshine Health?",
              "what is Molina's rate for H0031?",
              "is telehealth covered for H2017?"):
        assert not _asks_for_steps(q), f"wrongly procedural: {q!r}"


def test_the_question_text_reaches_the_shape_decision():
    """THE DEFECT ITSELF. `axis` is empty for procedural questions, so a
    QuestionShape carrying only an axis can never be procedural. This asserts
    the text field is what decides — pass the question with an EMPTY axis and
    it must still resolve to steps."""
    shaped = QuestionShape(text="how do i appeal a denial?", axis="")
    out = suggest_template(shaped)
    assert out and "numbered list" in out, (
        "the question text is not reaching _is_procedural — the procedural "
        "branch is unreachable again")


def test_an_explicit_request_still_outranks_the_question_shape():
    """A request you decline is a request you have to answer for. This is a
    procedural question AND an explicit table request; the request wins."""
    out = suggest_from_question("how do i appeal — show me a table")
    assert out and "table" in out.lower()


def test_the_documented_steering_phrases_actually_steer():
    """docs/product-docs/response-cards.md, authored by the UX team, tells
    users: "you can steer it by asking — 'show me a rate comparison table' or
    'give me the steps to appeal'". Both returned None: every pattern required
    "as/in/into a table", so a documented capability could not fire on its own
    documented example."""
    from app.responder.envelope_classifier import detect_explicit_format

    assert detect_explicit_format("show me a rate comparison table") == "table"
    assert detect_explicit_format("give me the steps to appeal") == "steps"


def test_mentioning_a_format_is_not_requesting_one():
    """The widening must not turn every sentence containing "table" into a
    table request — that would be the same defect pointing the other way."""
    from app.responder.envelope_classifier import detect_explicit_format

    for q in ("the steps below are wrong",
              "this table is missing a payer",
              "how do i appeal a CARC 22 denial?"):
        assert detect_explicit_format(q) is None, q
