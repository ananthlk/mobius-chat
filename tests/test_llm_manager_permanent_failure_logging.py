"""Chat Master's follow-up (2026-09-09) on the Task #108-adjacent permanent-
failure classifier (model_registry.classify_permanent_failure): a phrase-
matching heuristic that silently matches nothing on an unrecognized vendor
error shape has "the same shape as the bug you just fixed" — variant_id
matching nothing for a month with no signal anywhere (see
test_llm_analytics.py). llm_manager.generate()'s finally block now logs a
WARNING for every non-timeout failure the classifier does NOT recognize as
permanent, so a new failure-every-time shape shows up in logs immediately
instead of silently taking the slower statistical-threshold path.

Uses asyncio.run() directly, matching test_llm_manager_attachments.py's
documented reasoning (pytest-asyncio's loop fixture proved order-dependent
against other async test files in this suite).
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.llm_manager import generate


class _FakeSpec:
    model_id = "llama-3.3-70b-versatile"
    provider = "groq"


def _run_failing_generate(exc: Exception, caplog):
    fake_provider = MagicMock()
    fake_provider.generate_with_usage = AsyncMock(side_effect=exc)
    caplog.set_level(logging.WARNING, logger="app.services.llm_manager")
    with patch("app.services.model_registry.get_router") as mock_get_router:
        mock_get_router.return_value.select.return_value = (_FakeSpec(), {})
        with patch("app.services.llm_manager._provider_from_spec", return_value=fake_provider):
            with pytest.raises(Exception):
                asyncio.run(generate("hello", stage="react_1", max_tokens=100))


def test_unrecognized_failure_logs_warning(caplog):
    # A 404 shape the classifier doesn't recognize (at least not the one
    # this test asserts against) must log the "unrecognized failure shape"
    # WARNING so it's visible without waiting for a real incident.
    exc = Exception('Groq API error 404: {"error": {"message": "some new unrecognized vendor error"}}')
    _run_failing_generate(exc, caplog)
    assert any("unrecognized failure shape" in r.message for r in caplog.records)


def test_classified_permanent_failure_does_not_log_fallthrough_warning(caplog):
    # A recognized permanent failure (Anthropic's real live error text) is
    # already loud via the "permanent failure" degrade path elsewhere —
    # the fall-through WARNING here is specifically for the UNrecognized
    # case, so it must not also fire here.
    exc = Exception(
        'Anthropic API error 400: {"error": {"message": "Your credit '
        'balance is too low to access the Anthropic API."}}'
    )
    _run_failing_generate(exc, caplog)
    assert not any("unrecognized failure shape" in r.message for r in caplog.records)


def test_timeout_does_not_log_fallthrough_warning(caplog):
    # A timeout is a KNOWN failure shape (handled by the existing
    # is_timeout/window-based degradation) — not "unrecognized," so it
    # must not trigger the fall-through warning either.
    exc = TimeoutError("abandoned after 60s")
    _run_failing_generate(exc, caplog)
    assert not any("unrecognized failure shape" in r.message for r in caplog.records)
