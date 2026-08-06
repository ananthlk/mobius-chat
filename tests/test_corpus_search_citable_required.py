"""citable_required in corpus_search.py's request body to RAG (2026-08-05).

The chat-side keyword rule lives in react_loop.py (see
tests/test_react_citable_required.py); this locks in the other half —
that _post_skill() actually puts citable_required in the HTTP body sent
to corpus_search_agent when True, and omits the key entirely when False
(matching the Router's "omit means use your own default" contract, not
sending an explicit false).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from app.skills.builtin.corpus_search import _post_skill


def _mock_urlopen_capturing(captured: dict):
    def _urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        resp = MagicMock()
        resp.read.return_value = b'{"chunks": [], "telemetry": {}}'
        resp.__enter__ = lambda self: resp
        resp.__exit__ = lambda self, *a: None
        return resp
    return _urlopen


def test_citable_required_true_is_included_in_request_body():
    captured: dict = {}
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_capturing(captured)):
        _post_skill(
            base_url="https://rag.example.com",
            query="Is prior authorization required for H0036?",
            k=10,
            mode="corpus",
            filters={},
            include_document_ids=None,
            assembly_strategy=None,
            canonical_floor=None,
            caller_id="cid-1",
            citable_required=True,
        )
    assert captured["body"].get("citable_required") is True


def test_citable_required_false_is_omitted_from_request_body():
    captured: dict = {}
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_capturing(captured)):
        _post_skill(
            base_url="https://rag.example.com",
            query="What is the ICD-10 code for major depressive disorder?",
            k=10,
            mode="corpus",
            filters={},
            include_document_ids=None,
            assembly_strategy=None,
            canonical_floor=None,
            caller_id="cid-2",
            citable_required=False,
        )
    assert "citable_required" not in captured["body"]


def test_citable_required_defaults_to_omitted_when_not_passed():
    captured: dict = {}
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_capturing(captured)):
        _post_skill(
            base_url="https://rag.example.com",
            query="q",
            k=10,
            mode="corpus",
            filters={},
            include_document_ids=None,
            assembly_strategy=None,
            canonical_floor=None,
            caller_id="cid-3",
        )
    assert "citable_required" not in captured["body"]


def test_request_body_does_not_carry_allocator_override_or_authority_requirement():
    """Chat Architecture's ruling: those two stay off chat's request body
    entirely -- this is a negative-space regression guard, not exercised
    by _post_skill's params (it has none for them) but locks in that no
    future edit accidentally adds them here without a real decision."""
    captured: dict = {}
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen_capturing(captured)):
        _post_skill(
            base_url="https://rag.example.com",
            query="Is prior authorization required for H0036?",
            k=10,
            mode="corpus",
            filters={},
            include_document_ids=None,
            assembly_strategy=None,
            canonical_floor=None,
            caller_id="cid-4",
            citable_required=True,
        )
    assert "allocator_override" not in captured["body"]
    assert "mode_override" not in captured["body"]
    assert "authority_requirement" not in captured["body"]
