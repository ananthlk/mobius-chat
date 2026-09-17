"""The name resolution is GONE, and the condition it protected is guarded.

Chat used to resolve document_id -> documents.filename through
corpus_diagnostic before verifying, because verify-claims' reachability
precheck was keyed by FILENAME: a document whose curated display_name
differed by a trailing ".pdf" came back "is not in the corpus" while present
with 364,013 reachable characters.

Fixed upstream and verified on the SERVING revision 00005-l25 -- the commit
sat undeployed ~11 hours while two seats believed it was live. Probe:

    display name + id map    -> supported    1.0
    canonical filename       -> supported    1.0   (control)
    display name, NO id map  -> unverifiable "is not in the corpus"

The third line is the whole reason this file still exists: the upstream fix
is CONDITIONAL on the caller sending `document_ids`.
"""
import pathlib

from app.pipeline.v2 import verify as V
from app.pipeline.v2.contract import Fact


def test_the_local_resolution_is_gone():
    """Two name paths that can disagree is the drift we removed.

    Parsed, not grepped: the comment explaining WHY the resolution was
    removed names corpus_diagnostic, and a string search fails on the
    documentation. Reading prose instead of program is the defect I have
    now shipped four times in this suite.
    """
    import ast

    src = pathlib.Path(V.__file__).read_text()
    tree = ast.parse(src)
    assert not hasattr(V, "canonical_filenames"), "the resolver still exists"
    # No call anywhere passes corpus_diagnostic as a tool key.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for a in list(node.args) + [k.value for k in node.keywords]:
                if isinstance(a, ast.Constant) and a.value == "corpus_diagnostic":
                    raise AssertionError(
                        "chat still CALLS corpus_diagnostic to resolve a name")


def test_it_refuses_to_call_when_the_id_map_is_empty():
    """🔴 THE GUARD THE DELETION REQUIRED.

    Every sendable fact carries a document_id -- verifiable() drops the ones
    that do not. So an empty map here means something stopped populating it,
    and every claim is about to be reported ABSENT for a document we hold.
    That reads as a corpus problem and is a caller problem, which is the most
    expensive way for this to fail.
    """
    called = []

    def runner(tool, inputs):
        called.append(tool)
        return {"results": [{"verdict": "supported"}]}

    # A fact with an id, but verifiable()'s map suppressed: simulate by
    # sending a fact whose document name is absent from the map it builds.
    facts = [Fact("a claim", "Sunshine Provider Manual", 35, "d9721756")]
    original = V.verifiable

    def no_ids(f):
        sendable, _ids, excluded = original(f)
        return sendable, {}, excluded          # the defect: map lost

    V.verifiable = no_ids
    try:
        r = V.verify(facts, runner)
    finally:
        V.verifiable = original

    assert not called, "called the verifier with no id map"
    assert "document_ids is empty" in r.skipped
    assert r.findings == (), "a refusal to call is not a finding against a claim"


def test_the_normal_path_still_sends_the_id_map():
    sent = []

    def runner(tool, inputs):
        sent.append(inputs)
        return {"results": [{"verdict": "supported"}], "duration_ms": 1}

    V.verify([Fact("c", "Sunshine Provider Manual", 35, "d9721756")], runner)
    assert sent, "never called"
    assert sent[0].get("document_ids"), "id map missing from the payload"
    assert "Sunshine Provider Manual" in sent[0]["document_ids"]
