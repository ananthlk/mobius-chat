"""`.text` raises ValueError for TWO unrelated conditions -- a candidate with
NO parts, and a candidate with MORE THAN ONE part. The second is a completely
normal, successful response (finish_reason=STOP) that the SDK's single-string
accessor just can't return without a caller choosing how to join them.

Reported by Deep Research, relayed by Tool Manifest, 2026-09-11/12: a planner
call producing ~25k chars of output came back as 2-3 parts (STOP), and the
prior code's blanket `except ValueError` reported it as "response blocked --
no content parts" -- both halves of that message false (finish_reason was
STOP, and there WAS content, sitting in part 0). With Anthropic credit-
exhausted, every draw landed on Gemini and 7 of 7 planner calls in one run
died the same way.

Same shape as this file's own VertexTruncatedError fix (c622f0d,
test_vertex_truncation_vs_block.py): a guard written for one condition (no
parts) silently absorbed a second, unrelated one (many parts) into the same
`except ValueError`. Fixed by reading the parts directly instead of relying
on `.text` at all -- extract_vertex_text() is pure and public for the same
reason vertex_no_content_error() is: untestable inline, and a mutation that
deleted the branch would have left the plumbing tests green.
"""
from __future__ import annotations


class _FakePart:
    def __init__(self, text):
        self.text = text


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeCandidate:
    def __init__(self, parts, finish_reason="STOP"):
        self.content = _FakeContent(parts)
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, candidates):
        self.candidates = candidates


def test_single_part_joins_to_that_parts_text():
    from app.services.llm_provider import extract_vertex_text
    resp = _FakeResponse([_FakeCandidate([_FakePart("hello")])])
    assert extract_vertex_text(resp) == "hello"


def test_multiple_parts_join_in_order_not_discarded():
    # The exact reported shape: a long completion split across 2-3 parts,
    # finish_reason=STOP, that .text would have raised ValueError for.
    from app.services.llm_provider import extract_vertex_text
    resp = _FakeResponse([_FakeCandidate([
        _FakePart("first chunk of the answer, "),
        _FakePart("second chunk, "),
        _FakePart("third and final chunk."),
    ])])
    assert extract_vertex_text(resp) == (
        "first chunk of the answer, second chunk, third and final chunk."
    )


def test_no_parts_returns_none_not_an_exception():
    # The genuine no-content case -- callers use None to fall into
    # vertex_no_content_error's classification, unchanged from before.
    from app.services.llm_provider import extract_vertex_text
    resp = _FakeResponse([_FakeCandidate([], finish_reason="SAFETY")])
    assert extract_vertex_text(resp) is None


def test_no_candidates_returns_none():
    from app.services.llm_provider import extract_vertex_text
    resp = _FakeResponse([])
    assert extract_vertex_text(resp) is None


def test_part_with_no_text_attribute_is_skipped_not_fatal():
    # A function-call part or similar non-text part must not crash
    # extraction of the OTHER, real text parts alongside it.
    from app.services.llm_provider import extract_vertex_text

    class _NonTextPart:
        pass

    resp = _FakeResponse([_FakeCandidate([_NonTextPart(), _FakePart("actual text")])])
    assert extract_vertex_text(resp) == "actual text"


def test_malformed_response_object_falls_through_to_none():
    from app.services.llm_provider import extract_vertex_text
    assert extract_vertex_text(object()) is None
    assert extract_vertex_text(None) is None
