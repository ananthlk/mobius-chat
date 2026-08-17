"""Task #106 (2026-08-16, Ananth, directly): native document attachments
for Vertex/Gemini calls -- "these models are much better... if a doc is
small enough to send let's add those as attachments... we don't have to
build parsing." Step 1: the content-parts builder that converts a base64
attachment dict into a Gemini Part, leaving the plain-string prompt path
byte-for-byte unchanged when there's nothing to attach."""
from __future__ import annotations

import base64

from app.services.llm_provider import _vertex_content_parts


def test_no_attachments_returns_plain_string():
    assert _vertex_content_parts("hello", None) == "hello"


def test_empty_attachments_list_returns_plain_string():
    assert _vertex_content_parts("hello", []) == "hello"


def test_single_attachment_returns_list_with_prompt_first():
    data_b64 = base64.b64encode(b"fake pdf bytes").decode()
    out = _vertex_content_parts("hello", [{"mime_type": "application/pdf", "data_b64": data_b64}])
    assert isinstance(out, list)
    assert out[0] == "hello"
    assert len(out) == 2


def test_malformed_attachment_skipped_not_raised():
    out = _vertex_content_parts("hello", [{"mime_type": "application/pdf", "data_b64": "not-valid-base64!!!"}])
    # Malformed attachment is dropped; prompt survives as the sole part.
    assert out == ["hello"] or out == "hello"


def test_multiple_attachments_all_appended():
    data_b64 = base64.b64encode(b"doc bytes").decode()
    atts = [
        {"mime_type": "application/pdf", "data_b64": data_b64},
        {"mime_type": "application/pdf", "data_b64": data_b64},
    ]
    out = _vertex_content_parts("hello", atts)
    assert len(out) == 3  # prompt + 2 parts
