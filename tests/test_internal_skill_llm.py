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


# ── allowlist ↔ roster registration ──────────────────────────────────
# An allowlisted stage that is in NO model's eligible_stages passes the gate,
# gets zero candidates from _get_candidates, and falls through to
# fallback_no_models("gemini-2.5-flash"). The call SUCCEEDS — it just silently
# bypasses the bandit and its analytics, so nothing surfaces the gap. That is
# the documented PARALLEL_INTEGRATOR_STAGES bug (model_registry.py), where the
# same mistake ran unnoticed until someone went looking.

# Stages allowlisted but deliberately not yet routed. Each belongs to another
# seat and is reported to them; listing them here keeps the guard useful
# instead of permanently red, and makes the debt visible rather than silent.
_KNOWN_UNROUTED = {
    "appeals_investigation",
    "org_intel_report",
    "org_intel_synthesis",
    # payor_fact_reverify removed 2026-09-09 — routed at the stage owner's
    # request. The rot guard below is what forced this line to be deleted
    # rather than left behind as an exemption outliving its problem.
}


def _registered_stages() -> dict:
    from app.services.model_registry import MODEL_ROSTER
    reg: dict[str, list[str]] = {}
    for mid, spec in MODEL_ROSTER.items():
        for st in (spec.eligible_stages or []):
            reg.setdefault(st, []).append(mid)
    return reg


def test_every_allowlisted_stage_has_an_eligible_model():
    from app.main import _SKILL_LLM_ALLOWED_STAGES
    reg = _registered_stages()
    unrouted = {s for s in _SKILL_LLM_ALLOWED_STAGES if s not in reg}
    new = unrouted - _KNOWN_UNROUTED
    assert not new, (
        "these stages are allowlisted but no model declares them, so calls will "
        f"silently bypass the bandit: {sorted(new)}"
    )


def test_known_unrouted_list_does_not_rot():
    """If someone registers one of these, the entry must leave this list —
    otherwise the exemption outlives the problem and hides the next one."""
    reg = _registered_stages()
    fixed = {s for s in _KNOWN_UNROUTED if s in reg}
    assert not fixed, (
        f"now routed, remove from _KNOWN_UNROUTED: {sorted(fixed)}")


def test_research_parse_is_routable():
    from app.main import _SKILL_LLM_ALLOWED_STAGES
    assert "research_parse" in _SKILL_LLM_ALLOWED_STAGES
    assert _registered_stages().get("research_parse")


def test_response_schema_reaches_the_model_call():
    """Callers were sending response_schema before the field existed. Pydantic
    ignores unknown fields, so it was accepted and dropped — structured output
    worked against a caller's local dev fallback and would have silently become
    free text through chat, with no error to notice."""
    import asyncio
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    import app.main as m

    seen = {}

    async def _fake_generate(prompt, **kw):
        seen.update(kw)
        return "{}", {"model": "gemini-2.5-flash"}

    schema = {"type": "object", "properties": {"question": {"type": "string"}}}
    with patch.dict("os.environ", {"MOBIUS_SKILL_LLM_INTERNAL_KEY": "k"}), \
         patch.object(m.llm_manager if hasattr(m, "llm_manager") else __import__(
             "app.services.llm_manager", fromlist=["x"]), "generate", _fake_generate):
        c = TestClient(m.app)
        r = c.post("/internal/skill-llm",
                   headers={"X-Mobius-Skill-LLM-Key": "k"},
                   json={"system": "s", "user": "u", "stage": "research_parse",
                         "response_schema": schema})
    assert r.status_code == 200, r.text
    assert seen.get("response_schema") == schema, (
        "response_schema was accepted by the endpoint but never reached generate()")
