"""Retrieval and verification name the same document differently.

RAG's retrieval returns `document_name` (a display title). The verifier
matches on `documents.filename`. Usually identical -- the title IS the
filename. Where they differ, verification fails silently:

    Sunshine Provider Manual       -> "is not in the corpus"
    Sunshine Provider Manual.pdf   -> supported

Measured live: 5 of 13 sources on the appeals question were that document,
so the loss was concentrated on the most-used query and read as a verifier
fault rather than a naming one.
"""
from app.pipeline.v2 import verify as V
from app.pipeline.v2.contract import Fact

DID = "d9721756-d1b1-4cf4-845b-f44652c5fcf9"


def _runner(diag_docs, capture):
    def run(tool_key, inputs):
        if tool_key == V.NAME_TOOL_KEY:
            return {"documents": diag_docs}
        capture.append(inputs)
        return {"results": [{"verdict": "supported"} for _ in inputs["facts"]],
                "duration_ms": 1}
    return run


def test_the_display_title_is_replaced_by_the_corpus_filename():
    sent = []
    facts = [Fact("the deadline is 90 days", "Sunshine Provider Manual", 35, DID)]
    docs = [{"document_id": DID, "filename": "Sunshine Provider Manual.pdf"}]

    V.verify(facts, _runner(docs, sent))

    assert sent, "the verifier was never called"
    assert sent[0]["facts"][0]["document"] == "Sunshine Provider Manual.pdf"


def test_the_id_map_is_keyed_by_the_SAME_name_that_is_sent():
    """A payload naming one string and a scope map keyed by another is the
    unscopable case -- it costs the batch, not just the fact."""
    sent = []
    facts = [Fact("f", "Sunshine Provider Manual", 35, DID)]
    docs = [{"document_id": DID, "filename": "Sunshine Provider Manual.pdf"}]

    V.verify(facts, _runner(docs, sent))

    payload_names = {f["document"] for f in sent[0]["facts"]}
    assert payload_names <= set(sent[0]["document_ids"]), (
        f"names sent {payload_names} are not all keys of the id map "
        f"{set(sent[0]['document_ids'])}")


def test_a_name_that_already_matches_is_left_alone():
    sent = []
    facts = [Fact("f", "CMS-PRO-PE-Manual.pdf", 54, "abc")]
    docs = [{"document_id": "abc", "filename": "CMS-PRO-PE-Manual.pdf"}]

    V.verify(facts, _runner(docs, sent))
    assert sent[0]["facts"][0]["document"] == "CMS-PRO-PE-Manual.pdf"


def test_it_asks_the_corpus_rather_than_guessing_an_extension():
    """Appending '.pdf' would pass the first test and be wrong: it guesses a
    convention instead of asking the system that owns the name."""
    sent = []
    facts = [Fact("f", "Some Handbook", 3, "zzz")]
    # the corpus knows it by something an extension rule would never produce
    docs = [{"document_id": "zzz", "filename": "2024_some_handbook_v3.docx"}]

    V.verify(facts, _runner(docs, sent))
    assert sent[0]["facts"][0]["document"] == "2024_some_handbook_v3.docx"


def test_an_unresolvable_id_keeps_its_original_name():
    sent = []
    facts = [Fact("f", "Unknown Doc", 1, "nope")]
    V.verify(facts, _runner([], sent))
    assert sent[0]["facts"][0]["document"] == "Unknown Doc"


def test_a_failing_resolver_never_fails_the_turn():
    """Improving a name must never cost the verification, let alone the
    answer."""
    sent = []

    def run(tool_key, inputs):
        if tool_key == V.NAME_TOOL_KEY:
            raise RuntimeError("corpus_diagnostic down")
        sent.append(inputs)
        return {"results": [{"verdict": "supported"}], "duration_ms": 1}

    r = V.verify([Fact("f", "Doc", 1, "x")], run)
    assert r.supported == 1
    assert sent[0]["facts"][0]["document"] == "Doc"
