"""Task #106 (2026-08-16, Ananth, directly): document attachments threaded
through llm_manager.generate() -> provider.generate_with_usage(). Verifies
the attachments kwarg is passed only when present, so every existing
provider/call path that never uses attachments is byte-for-byte unchanged
(Ollama's generate_with_usage blind-forwards **kwargs into _ollama_request,
which would break on an unexpected always-present `attachments=None` key).

Uses asyncio.run() directly rather than @pytest.mark.asyncio -- the latter's
event-loop fixture proved order-dependent against other async test files in
this suite (crashed at the C level when collected after
test_llm_provider_exhausted.py, passed cleanly in isolation or reverse
order) -- a pytest/asyncio-loop interaction, not a bug in the code under
test. asyncio.run() owns its own loop end-to-end per test, sidestepping it.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.llm_manager import generate
from app.services.usage import zero_usage


class _FakeSpec:
    model_id = "gemini-2.5-flash"
    provider = "vertex"


def test_no_attachments_omits_kwarg_entirely():
    fake_provider = MagicMock()
    fake_provider.generate_with_usage = AsyncMock(return_value=("answer", zero_usage("vertex", "gemini-2.5-flash")))
    with patch("app.services.model_registry.get_router") as mock_get_router:
        mock_get_router.return_value.select.return_value = (_FakeSpec(), {})
        with patch("app.services.llm_manager._provider_from_spec", return_value=fake_provider):
            asyncio.run(generate("hello", stage="react_1", max_tokens=100))
    _, kwargs = fake_provider.generate_with_usage.call_args
    assert "attachments" not in kwargs


def test_attachments_passed_when_present():
    fake_provider = MagicMock()
    fake_provider.generate_with_usage = AsyncMock(return_value=("answer", zero_usage("vertex", "gemini-2.5-flash")))
    atts = [{"mime_type": "application/pdf", "data_b64": "ZmFrZQ=="}]
    with patch("app.services.model_registry.get_router") as mock_get_router:
        mock_get_router.return_value.select.return_value = (_FakeSpec(), {})
        with patch("app.services.llm_manager._provider_from_spec", return_value=fake_provider):
            asyncio.run(generate("hello", stage="react_1", max_tokens=100, attachments=atts))
    _, kwargs = fake_provider.generate_with_usage.call_args
    assert kwargs.get("attachments") == atts
