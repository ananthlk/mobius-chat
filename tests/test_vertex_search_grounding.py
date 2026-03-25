"""Unit tests for Vertex AI Search (Discovery Engine) grounding helpers."""
import os

import pytest

from app.services.llm_provider import (
    expand_vertex_ai_search_datastore_path,
    should_attach_vertex_search_grounding,
)


def test_expand_path_explicit() -> None:
    p = "projects/foo/locations/global/collections/default_collection/dataStores/bar"
    assert expand_vertex_ai_search_datastore_path(p, project_id="ignored") == p


def test_expand_path_from_store_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERTEX_AI_SEARCH_DATASTORE", raising=False)
    monkeypatch.setenv("VERTEX_AI_SEARCH_DATASTORE_ID", "my-store")
    monkeypatch.setenv("VERTEX_AI_SEARCH_LOCATION", "global")
    out = expand_vertex_ai_search_datastore_path("", project_id="mobius-os-dev")
    assert (
        out
        == "projects/mobius-os-dev/locations/global/collections/default_collection/dataStores/my-store"
    )


def test_grounding_mode_credentialing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERTEX_AI_SEARCH_GROUNDING_MODE", "credentialing")
    assert should_attach_vertex_search_grounding("credentialing_draft") is True
    assert should_attach_vertex_search_grounding("planner") is False
    assert should_attach_vertex_search_grounding("integrator") is False


def test_grounding_mode_general(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERTEX_AI_SEARCH_GROUNDING_MODE", "general")
    assert should_attach_vertex_search_grounding(None) is True
    assert should_attach_vertex_search_grounding("") is True
    assert should_attach_vertex_search_grounding("planner") is False
    assert should_attach_vertex_search_grounding("react_1") is False
    assert should_attach_vertex_search_grounding("integrator") is False
    assert should_attach_vertex_search_grounding("rag") is False


def test_grounding_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERTEX_AI_SEARCH_GROUNDING_MODE", "off")
    assert should_attach_vertex_search_grounding("credentialing_draft") is False
