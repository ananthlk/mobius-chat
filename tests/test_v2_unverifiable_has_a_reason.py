"""An unverifiable count with no reason is a could-not-check with no tell.

The verifier returns a `why` on EVERY unverifiable row. This module counted
them and dropped the reason, so a live turn reported "unverifiable=8" and the
only way to learn why was to call the service by hand. The answer was one
line -- "<document> is not in the corpus" -- and it had been returned on all
eight rows, every turn, and discarded each time.

A reason returned and discarded is worse than one never produced: it makes
the gap look unexplainable when it is merely unrecorded.
"""
from app.pipeline.v2 import verify as V


class _F:
    def __init__(self, fact, document, page, document_id="d1"):
        self.fact, self.document, self.page = fact, document, page
        self.document_id = document_id


def _runner_returning(rows):
    def run(tool_key, inputs):
        return {"results": rows, "duration_ms": 5}
    return run


def test_the_reason_survives_the_count():
    facts = [_F("a claim", "Sunshine Provider Manual", 35)]
    rows = [{"verdict": "unverifiable",
             "why": "Sunshine Provider Manual is not in the corpus"}]
    r = V.verify(facts, _runner_returning(rows))

    assert r.unverifiable == 1
    assert r.unverifiable_why, "the reason was dropped -- the whole defect"
    why, n = r.unverifiable_why[0]
    assert n == 1
    assert "not in the corpus" in why


def test_the_document_name_is_normalised_so_reasons_GROUP():
    """The document varies per row, the failure does not. Without
    normalisation five rows produce five distinct 'reasons' and nothing
    aggregates -- which is how a systematic cause reads as noise."""
    facts = [_F("c1", "Doc A", 1), _F("c2", "Doc B", 2)]
    rows = [{"verdict": "unverifiable", "why": "Doc A is not in the corpus"},
            {"verdict": "unverifiable", "why": "Doc B is not in the corpus"}]
    r = V.verify(facts, _runner_returning(rows))

    assert len(r.unverifiable_why) == 1, (
        f"two rows with one cause produced {len(r.unverifiable_why)} reasons: "
        f"{r.unverifiable_why}")
    why, n = r.unverifiable_why[0]
    assert n == 2
    assert "<document>" in why


def test_a_missing_reason_is_named_not_blank():
    facts = [_F("c", "Doc", 1)]
    rows = [{"verdict": "unverifiable"}]
    r = V.verify(facts, _runner_returning(rows))
    assert r.unverifiable_why[0][0] == "no reason given"


def test_reasons_are_ordered_most_common_first():
    facts = [_F(f"c{i}", f"Doc {i}", i) for i in range(3)]
    rows = [{"verdict": "unverifiable", "why": "Doc 0 is not in the corpus"},
            {"verdict": "unverifiable", "why": "Doc 1 is not in the corpus"},
            {"verdict": "unverifiable", "why": "page 2 is beyond the document"}]
    r = V.verify(facts, _runner_returning(rows))
    assert r.unverifiable_why[0][1] == 2


def test_supported_rows_contribute_no_reason():
    facts = [_F("c", "Doc", 1)]
    rows = [{"verdict": "supported", "score": 0.9}]
    r = V.verify(facts, _runner_returning(rows))
    assert r.supported == 1
    assert r.unverifiable == 0
    assert r.unverifiable_why == ()
