"""Visibility-keyed access control on /chat/uploads/{id}/download.

PHI-policy gate contract (PHI agent ruling 2026-07-20): private →
owner-only, unconditionally (not gated on auth_mode); org → owner-only
stand-in until org identity lands; public → open; absent identity on a
non-public row → 403; absent/unknown visibility → private (fail-closed).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.download as dl
from app.api.front_door import require_user


ROW = {
    "document_id": "doc-1",
    "user_id": "alice",
    "filename": "john_doe_treatment_plan.pdf",
    "status": "active",
}


def _client(monkeypatch, row, caller):
    monkeypatch.setattr(dl, "get_by_document_id", lambda _id: dict(row) if row else None)
    monkeypatch.setattr(
        dl, "_stream_upstream",
        lambda url, filename: {"streamed": True, "filename": filename},
    )
    monkeypatch.setenv("RAG_API_URL", "https://rag.example")
    app = FastAPI()
    app.include_router(dl.router)
    app.dependency_overrides[require_user] = lambda: caller
    return TestClient(app)


def test_row_visibility_fail_closed():
    assert dl._row_visibility({}) == "private"
    assert dl._row_visibility({"suggested_visibility": "org"}) == "org"
    assert dl._row_visibility({"suggested_visibility": "org", "confirmed_visibility": "public"}) == "public"
    assert dl._row_visibility({"confirmed_visibility": "weird"}) == "private"


@pytest.mark.parametrize("vis", [None, "private", "org"])
def test_non_public_denied_without_identity(monkeypatch, vis):
    row = {**ROW}
    if vis:
        row["confirmed_visibility"] = vis
    c = _client(monkeypatch, row, caller=None)
    r = c.get("/chat/uploads/doc-1/download")
    assert r.status_code == 403


def test_private_denied_for_non_owner(monkeypatch):
    c = _client(monkeypatch, {**ROW, "confirmed_visibility": "private"}, caller="bob")
    assert c.get("/chat/uploads/doc-1/download").status_code == 403


def test_private_owner_allowed(monkeypatch):
    c = _client(monkeypatch, {**ROW, "confirmed_visibility": "private"}, caller="alice")
    r = c.get("/chat/uploads/doc-1/download")
    assert r.status_code == 200
    assert r.json()["streamed"] is True


def test_legacy_row_without_owner_fails_closed(monkeypatch):
    row = {**ROW, "user_id": None}
    c = _client(monkeypatch, row, caller="alice")
    assert c.get("/chat/uploads/doc-1/download").status_code == 403


def test_public_open_without_identity(monkeypatch):
    c = _client(monkeypatch, {**ROW, "confirmed_visibility": "public"}, caller=None)
    assert c.get("/chat/uploads/doc-1/download").status_code == 200


def test_missing_row_404(monkeypatch):
    c = _client(monkeypatch, None, caller="alice")
    assert c.get("/chat/uploads/doc-1/download").status_code == 404


def test_log_line_has_no_filename(monkeypatch, caplog):
    import logging
    c = _client(monkeypatch, {**ROW, "confirmed_visibility": "public"}, caller=None)
    with caplog.at_level(logging.INFO, logger="app.api.download"):
        c.get("/chat/uploads/doc-1/download")
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "john_doe_treatment_plan" not in joined
    assert "ext=pdf" in joined
