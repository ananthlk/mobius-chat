"""Credentialing run service streams orchestrator emits when emitter is provided."""

from __future__ import annotations

import pytest

from app.services.credentialing_run_service import clear_runs_for_tests, create_credentialing_run


@pytest.fixture(autouse=True)
def _clear_runs():
    clear_runs_for_tests()
    yield
    clear_runs_for_tests()


def test_copilot_create_emits_progress(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", raising=False)
    lines: list[str] = []
    create_credentialing_run("EmitOrg", "copilot", thread_id=None, emitter=lines.append)
    assert len(lines) >= 1
    joined = "\n".join(lines)
    assert "Steps:" in joined or "Step" in joined or "revenue" in joined.lower() or "skipped" in joined.lower()


def test_autopilot_emits_progress(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", raising=False)
    lines: list[str] = []
    create_credentialing_run("EmitOrg", "autopilot", thread_id=None, emitter=lines.append)
    assert len(lines) >= 1
