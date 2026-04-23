"""Unit tests for the cached_answer_lookup skill.

Mocks the Chroma collection + embedding provider so these tests run
fast and offline. Integration against a real Chroma lives separately
(run manually post-deploy).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.skills.builtin import cached_answer as skill_mod
from app.skills.registry import SkillCall


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_collection_cache():
    """Each test gets a fresh collection mock; reset the module-level cache."""
    skill_mod._reset_collection_cache()
    yield
    skill_mod._reset_collection_cache()


def _iso_days_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat(timespec="seconds")


def _fake_chroma_result(candidates: list[dict]):
    """Build a Chroma query() response from a list of candidate dicts.

    Each candidate is {id, similarity, meta, document}."""
    ids = [[c["id"] for c in candidates]]
    metas = [[c.get("meta") or {} for c in candidates]]
    docs = [[c.get("document", "") for c in candidates]]
    # Chroma returns cosine distance = 1 - similarity
    dists = [[max(0.0, min(2.0, 1.0 - c["similarity"])) for c in candidates]]
    return {
        "ids": ids,
        "metadatas": metas,
        "documents": docs,
        "distances": dists,
    }


def _install_mock_collection(candidates: list[dict]):
    """Helper: replaces the module's collection with a MagicMock whose
    .query() returns the given candidates."""
    coll = MagicMock()
    coll.query.return_value = _fake_chroma_result(candidates)
    # Plant it directly so _get_cache_collection returns the mock.
    skill_mod._CACHE_COLLECTION = coll
    return coll


def _mock_embedding_provider(monkeypatch, dims: int = 1536):
    """Stub embedding provider so tests don't hit Vertex."""
    def fake_embed(text: str):
        return [0.1] * dims
    monkeypatch.setattr(
        "app.services.embedding_provider.get_query_embedding", fake_embed
    )


# ── Input parsing ─────────────────────────────────────────────────────


def test_parse_inputs_defaults():
    out = skill_mod._parse_inputs({})
    assert out["similarity_floor"] == 0.82
    assert out["max_age_days"] is None  # no skill-level default; caller supplies
    assert out["top_k"] == 3
    assert out["require_no_thumbs_down"] is True
    assert out["require_critic_approved"] is False
    assert out["domain_tags"] is None
    assert out["config_sha"] is None


def test_parse_inputs_clamps_similarity_floor():
    out = skill_mod._parse_inputs({"similarity_floor": 1.5})
    assert out["similarity_floor"] == 1.0
    out = skill_mod._parse_inputs({"similarity_floor": -0.2})
    assert out["similarity_floor"] == 0.0


def test_parse_inputs_handles_string_domain_tags():
    out = skill_mod._parse_inputs({"domain_tags": "policy, fl_medicaid"})
    assert out["domain_tags"] == ["policy", "fl_medicaid"]


def test_parse_inputs_handles_list_domain_tags():
    out = skill_mod._parse_inputs({"domain_tags": ["policy", "  ", "fl_medicaid"]})
    assert out["domain_tags"] == ["policy", "fl_medicaid"]


def test_parse_inputs_tolerates_garbage():
    out = skill_mod._parse_inputs({
        "similarity_floor": "not_a_float",
        "max_age_days": [],
        "top_k": "abc",
    })
    # All fall back to defaults without raising
    assert out["similarity_floor"] == 0.82
    assert out["max_age_days"] is None
    assert out["top_k"] == 3


# ── Filter plumbing ───────────────────────────────────────────────────


def test_build_where_default_includes_thumbs_down_filter():
    """``require_no_thumbs_down=True`` is the skill's default, so the
    default where clause excludes downvoted cache entries unless the
    caller explicitly opts out."""
    filters = skill_mod._parse_inputs({})
    where = skill_mod._build_chroma_where(filters)
    assert where == {"thumbs_down": {"$ne": True}}


def test_build_where_skipped_when_caller_opts_out():
    filters = skill_mod._parse_inputs({"require_no_thumbs_down": False})
    assert skill_mod._build_chroma_where(filters) is None


def test_build_where_critic_approved_plus_default_thumbs_filter():
    filters = skill_mod._parse_inputs({"require_critic_approved": True})
    where = skill_mod._build_chroma_where(filters)
    # Both the explicit critic_approved filter AND the default
    # thumbs_down filter land under $and.
    assert "$and" in where
    conditions = where["$and"]
    assert {"critic_approved": True} in conditions
    assert {"thumbs_down": {"$ne": True}} in conditions


def test_build_where_combined():
    filters = skill_mod._parse_inputs({
        "require_critic_approved": True,
        "quality_score_floor": 0.5,
        "config_sha": "abc123",
    })
    where = skill_mod._build_chroma_where(filters)
    assert "$and" in where
    conditions = where["$and"]
    assert {"critic_approved": True} in conditions
    assert {"thumbs_down": {"$ne": True}} in conditions
    assert {"quality_score": {"$gte": 0.5}} in conditions
    assert {"config_sha": "abc123"} in conditions


# ── Post-filter (age + domain + similarity) ───────────────────────────


def test_post_filter_drops_below_similarity_floor():
    candidates = [
        {"similarity": 0.90, "age_days": 2.0, "meta": {}},
        {"similarity": 0.70, "age_days": 2.0, "meta": {}},
    ]
    filters = skill_mod._parse_inputs({"similarity_floor": 0.82})
    kept, reasons = skill_mod._post_filter(candidates, filters)
    assert len(kept) == 1
    assert kept[0]["similarity"] == 0.90
    assert reasons.get("below_similarity_floor") == 1


def test_post_filter_drops_over_max_age():
    candidates = [
        {"similarity": 0.90, "age_days": 30.0, "meta": {}},
        {"similarity": 0.90, "age_days": 5.0, "meta": {}},
    ]
    filters = skill_mod._parse_inputs({"max_age_days": 14})
    kept, reasons = skill_mod._post_filter(candidates, filters)
    assert len(kept) == 1
    assert kept[0]["age_days"] == 5.0
    assert reasons.get("age_over_threshold") == 1


def test_post_filter_drops_on_domain_tag_mismatch():
    candidates = [
        {"similarity": 0.90, "age_days": 1.0, "meta": {"domain_tags": "state:fl"}},
        {"similarity": 0.90, "age_days": 1.0, "meta": {"domain_tags": "state:tx"}},
    ]
    filters = skill_mod._parse_inputs({"domain_tags": ["state:fl"]})
    kept, reasons = skill_mod._post_filter(candidates, filters)
    assert len(kept) == 1
    assert "state:fl" in kept[0]["meta"]["domain_tags"]
    assert reasons.get("domain_tag_mismatch") == 1


def test_post_filter_keeps_when_any_tag_matches():
    candidates = [
        {"similarity": 0.9, "age_days": 1.0,
         "meta": {"domain_tags": "payer:sunshine_health,state:fl"}},
    ]
    filters = skill_mod._parse_inputs({"domain_tags": ["state:fl"]})
    kept, _ = skill_mod._post_filter(candidates, filters)
    assert len(kept) == 1


# ── Age helper ────────────────────────────────────────────────────────


def test_age_days_of_parses_iso_z_suffix():
    iso = _iso_days_ago(2.5).replace("+00:00", "Z")
    assert skill_mod._age_days_of(iso) is not None
    assert 2.0 < skill_mod._age_days_of(iso) < 3.0


def test_age_days_of_returns_none_on_bad_input():
    assert skill_mod._age_days_of(None) is None
    assert skill_mod._age_days_of("") is None
    assert skill_mod._age_days_of("not a timestamp") is None


# ── Full handler (end-to-end with mocked collection + embedder) ─────


def test_handler_returns_empty_envelope_on_no_query_text(monkeypatch):
    _mock_embedding_provider(monkeypatch)
    _install_mock_collection([])
    env = skill_mod._run(SkillCall(name="cached_answer_lookup", inputs={}, question=""))
    assert env.signal == "no_sources"
    assert env.extra.get("reasons_filtered", {}).get("empty_query") == 1


def test_handler_returns_hits_above_threshold(monkeypatch):
    _mock_embedding_provider(monkeypatch)
    _install_mock_collection([
        {
            "id": "cid-good",
            "similarity": 0.95,
            "document": "What is the PA timeline?",
            "meta": {
                "question": "What is the PA timeline?",
                "final_message": "Sunshine Health requires 14 days.",
                "source_count": 3,
                "created_at": _iso_days_ago(2),
                "critic_approved": True,
                "config_sha": "abc",
            },
        },
        {
            "id": "cid-weak",
            "similarity": 0.60,
            "document": "Unrelated question",
            "meta": {
                "question": "Unrelated question",
                "final_message": "Off topic",
                "created_at": _iso_days_ago(1),
            },
        },
    ])
    env = skill_mod._run(SkillCall(
        name="cached_answer_lookup",
        inputs={"question": "What is the PA timeline?"},
        question="What is the PA timeline?",
    ))
    assert env.signal == "cache_hit"
    assert len(env.sources) == 1
    assert env.sources[0].document_name == "cached_answer[1]"
    assert "Sunshine Health requires 14 days" in env.text
    cands = env.extra["candidates"]
    assert len(cands) == 1
    assert cands[0]["cache_turn_id"] == "cid-good"
    assert cands[0]["similarity"] == pytest.approx(0.95, abs=1e-6)
    assert env.extra["reasons_filtered"].get("below_similarity_floor") == 1


def test_handler_empties_when_age_over_caller_supplied_max(monkeypatch):
    _mock_embedding_provider(monkeypatch)
    _install_mock_collection([
        {
            "id": "stale",
            "similarity": 0.95,
            "document": "q",
            "meta": {
                "question": "q",
                "final_message": "stale answer",
                "created_at": _iso_days_ago(30),
            },
        },
    ])
    env = skill_mod._run(SkillCall(
        name="cached_answer_lookup",
        inputs={"question": "q", "max_age_days": 7},
        question="q",
    ))
    assert env.signal == "no_sources"
    assert env.extra["reasons_filtered"].get("age_over_threshold") == 1


def test_handler_survives_collection_unavailable(monkeypatch):
    """If Chroma is down, return an empty envelope rather than crashing."""
    _mock_embedding_provider(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(skill_mod, "_get_cache_collection", boom)

    env = skill_mod._run(SkillCall(
        name="cached_answer_lookup", inputs={"question": "q"}, question="q",
    ))
    assert env.signal == "no_sources"
    assert env.extra["reasons_filtered"].get("collection_unavailable") == 1


def test_handler_survives_embed_failure(monkeypatch):
    _install_mock_collection([])

    def boom(text: str):
        raise RuntimeError("Vertex 429")
    monkeypatch.setattr(
        "app.services.embedding_provider.get_query_embedding", boom
    )

    env = skill_mod._run(SkillCall(
        name="cached_answer_lookup", inputs={"question": "q"}, question="q",
    ))
    assert env.signal == "no_sources"
    assert env.extra["reasons_filtered"].get("embed_failed") == 1


def test_handler_respects_top_k(monkeypatch):
    _mock_embedding_provider(monkeypatch)
    _install_mock_collection([
        {"id": f"c{i}", "similarity": 0.90 - i * 0.01,
         "document": f"q{i}",
         "meta": {"question": f"q{i}", "final_message": f"a{i}",
                  "created_at": _iso_days_ago(1)}}
        for i in range(10)
    ])
    env = skill_mod._run(SkillCall(
        name="cached_answer_lookup",
        inputs={"question": "q", "top_k": 2},
        question="q",
    ))
    assert len(env.extra["candidates"]) == 2
    # Should be the two highest-similarity candidates
    assert env.extra["candidates"][0]["cache_turn_id"] == "c0"
    assert env.extra["candidates"][1]["cache_turn_id"] == "c1"


# ── Registration ──────────────────────────────────────────────────────


def test_spec_is_registered():
    from app.skills import registry
    assert registry.has("cached_answer_lookup")
    spec = registry.get("cached_answer_lookup")
    assert spec.source == "builtin"
    assert spec.visible_to_planner is True
    # Agentic mode explicitly excluded per product policy.
    assert "agentic" not in spec.supports_modes
    assert "copilot" in spec.supports_modes
    assert "quick" in spec.supports_modes
