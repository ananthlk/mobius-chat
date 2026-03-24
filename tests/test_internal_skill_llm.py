"""POST /internal/skill-llm — credentialing → chat dynamic router."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_internal_skill_llm_503_when_key_unset(client, monkeypatch):
    monkeypatch.delenv("MOBIUS_SKILL_LLM_INTERNAL_KEY", raising=False)
    r = client.post(
        "/internal/skill-llm",
        json={"system": "s", "user": "u", "stage": "credentialing_draft", "max_tokens": 10},
        headers={"X-Mobius-Skill-LLM-Key": "any"},
    )
    assert r.status_code == 503


def test_internal_skill_llm_401_wrong_key(client, monkeypatch):
    monkeypatch.setenv("MOBIUS_SKILL_LLM_INTERNAL_KEY", "secret-a")
    r = client.post(
        "/internal/skill-llm",
        json={"system": "s", "user": "u", "stage": "credentialing_draft", "max_tokens": 10},
        headers={"X-Mobius-Skill-LLM-Key": "secret-b"},
    )
    assert r.status_code == 401


def test_internal_skill_llm_400_bad_stage(client, monkeypatch):
    monkeypatch.setenv("MOBIUS_SKILL_LLM_INTERNAL_KEY", "ok")
    r = client.post(
        "/internal/skill-llm",
        json={"system": "s", "user": "u", "stage": "not_a_real_stage", "max_tokens": 10},
        headers={"X-Mobius-Skill-LLM-Key": "ok"},
    )
    assert r.status_code == 400


def test_internal_skill_llm_ok(client, monkeypatch):
    monkeypatch.setenv("MOBIUS_SKILL_LLM_INTERNAL_KEY", "ok")

    async def fake_generate(*args, **kwargs):
        return ("hello", {"model": "gemini-2.5-flash", "provider": "vertex"})

    with patch("app.services.llm_manager.generate", new=AsyncMock(side_effect=fake_generate)):
        r = client.post(
            "/internal/skill-llm",
            json={
                "system": "You are a test",
                "user": "Say hi",
                "stage": "credentialing_validate",
                "max_tokens": 50,
                "correlation_id": "corr-1",
            },
            headers={"X-Mobius-Skill-LLM-Key": "ok"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("text") == "hello"
    assert body.get("usage", {}).get("model") == "gemini-2.5-flash"
