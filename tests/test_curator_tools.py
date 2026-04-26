"""Tests for app.pipeline.curator_tools — the ReAct tool handlers
that bridge chat to rag's /sources/* and /documents/import-from-html
endpoints (Phase 13.5).

These cover the contract a planner sees: input shape, return shape,
error paths, and the prose summary that goes back to the model.
HTTP is mocked via a small fake httpx.Client so tests are network-free.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch, MagicMock

import pytest


def _import_under_test():
    """Import inside the test so monkey-patches on httpx land first."""
    from app.pipeline import curator_tools  # noqa: WPS433 — intentional
    return curator_tools


# ── Fake httpx.Client ────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, json_body: Any | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_body
        self.text = text or (json.dumps(json_body) if json_body is not None else "")

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            req = httpx.Request("GET", "http://test")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=req,
                response=httpx.Response(self.status_code, text=self.text, request=req),
            )


class _FakeClient:
    """Minimal httpx.Client stand-in. Records calls so tests can assert."""

    def __init__(self, responses: dict[tuple[str, str], _FakeResponse]):
        self._responses = responses
        self.calls: list[tuple[str, str, dict | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        # exact match first; else any GET response
        key = ("GET", url)
        if key in self._responses:
            return self._responses[key]
        return self._responses.get(("GET", "*"), _FakeResponse(404))

    def post(self, url, json=None, headers=None):
        self.calls.append(("POST", url, json))
        key = ("POST", url)
        if key in self._responses:
            return self._responses[key]
        return self._responses.get(("POST", "*"), _FakeResponse(404))


# ── lookup_authoritative_sources ─────────────────────────────────────


def _set_rag_url(monkeypatch, url="https://rag.test"):
    monkeypatch.setenv("RAG_API_URL", url)
    monkeypatch.delenv("RAG_API_BASE", raising=False)


def test_lookup_returns_no_sources_when_no_rag_url():
    """When _rag_base() returns empty (no RAG_API_URL configured),
    return a clean error result instead of raising — chat keeps
    working in deploys that don't have the curator wired.

    We patch ``_rag_base`` directly rather than fighting chat's
    auto-loaded .env (which sets RAG_API_URL=localhost:8030 for dev).
    """
    ct = _import_under_test()
    with patch.object(ct, "_rag_base", return_value=""):
        out = ct.call_lookup_authoritative_sources({"payer": "Sunshine Health"})
    assert out["success"] is False
    assert out["tool"] == "lookup_authoritative_sources"
    assert "RAG_API_URL not configured" in out["result"]
    assert out["signal"] == "no_sources"


def test_lookup_renders_prose_summary(monkeypatch):
    """Happy path — server returns 2 rows, we get a planner-readable
    summary with both ✓ indexed and ○ NOT indexed flags."""
    _set_rag_url(monkeypatch)
    rows = [
        {"url": "https://sun.com/a.html", "host": "sun.com", "payer": "Sunshine Health",
         "state": "FL", "ingested": True, "last_seen_at": "2026-04-25T03:00:00",
         "effective_authority_level": "payer_manual"},
        {"url": "https://sun.com/b.html", "host": "sun.com", "payer": "Sunshine Health",
         "state": "FL", "ingested": False, "last_seen_at": "2026-04-25T03:00:00",
         "effective_authority_level": "payer_policy"},
    ]
    fake = _FakeClient({("GET", "https://rag.test/sources/search"): _FakeResponse(200, rows)})
    with patch("httpx.Client", return_value=fake):
        ct = _import_under_test()
        out = ct.call_lookup_authoritative_sources({"payer": "Sunshine Health"})
    assert out["success"] is True
    assert out["rows"] == rows
    # Prose summary mentions count + ingested flag for the planner.
    text = out["result"]
    assert "2 URL(s)" in text
    assert "✓ indexed" in text
    assert "○ NOT indexed" in text
    assert "1 of these are NOT yet in the corpus" in text


def test_lookup_passes_filters_through_query_params(monkeypatch):
    """payer/state/topic/authority_level all forwarded as query params,
    plus only_reachable=true and limit=20 always."""
    _set_rag_url(monkeypatch)
    fake = _FakeClient({("GET", "https://rag.test/sources/search"): _FakeResponse(200, [])})
    with patch("httpx.Client", return_value=fake):
        ct = _import_under_test()
        ct.call_lookup_authoritative_sources({
            "payer": "Sunshine Health",
            "state": "FL",
            "topic": "ECT",
            "authority_level": "payer_policy",
        })
    assert len(fake.calls) == 1
    method, url, params = fake.calls[0]
    assert method == "GET"
    assert params["payer"] == "Sunshine Health"
    assert params["state"] == "FL"
    assert params["topic"] == "ECT"
    assert params["authority_level"] == "payer_policy"
    assert params["only_reachable"] == "true"
    assert params["limit"] == 20


def test_lookup_handles_5xx_cleanly(monkeypatch):
    """Server error returns a failure dict, never raises."""
    _set_rag_url(monkeypatch)
    fake = _FakeClient({("GET", "https://rag.test/sources/search"): _FakeResponse(500, text="boom")})
    with patch("httpx.Client", return_value=fake):
        ct = _import_under_test()
        out = ct.call_lookup_authoritative_sources({"payer": "X"})
    assert out["success"] is False
    assert "HTTP 500" in out["result"]


def test_lookup_empty_result_returns_clean_message(monkeypatch):
    """Zero results — planner gets a clear no-sources message and the
    rows list is empty (so it doesn't try to dereference)."""
    _set_rag_url(monkeypatch)
    fake = _FakeClient({("GET", "https://rag.test/sources/search"): _FakeResponse(200, [])})
    with patch("httpx.Client", return_value=fake):
        ct = _import_under_test()
        out = ct.call_lookup_authoritative_sources({"payer": "Unknown"})
    assert out["success"] is True
    assert out["rows"] == []
    assert "no matching sources" in out["result"].lower()


# ── ingest_url ───────────────────────────────────────────────────────


def test_ingest_url_requires_url_input():
    """Empty inputs → fail-fast with a planner-readable message,
    BEFORE any HTTP work happens. Order matters because some deploys
    have RAG_API_URL unset; we want missing-url to be the more
    actionable error."""
    ct = _import_under_test()
    out = ct.call_ingest_url({})
    assert out["success"] is False
    assert "requires a 'url' input" in out["result"]


def test_ingest_url_returns_clean_when_no_rag_configured():
    """When rag isn't wired, ingest_url with a real URL still fails
    cleanly via the no-rag-url path."""
    ct = _import_under_test()
    with patch.object(ct, "_rag_base", return_value=""):
        out = ct.call_ingest_url({"url": "https://x.com/y"})
    assert out["success"] is False
    assert "RAG_API_URL not configured" in out["result"]


def test_ingest_url_happy_path(monkeypatch):
    """Server returns 200 with document_id — success result tells the
    planner to call search_corpus next."""
    _set_rag_url(monkeypatch)
    body = {
        "url": "https://sun.com/policy.html",
        "title": "Some Policy",
        "document_id": "abc-123",
        "status": "completed",
        "sections": 4,
    }
    fake = _FakeClient({
        ("POST", "https://rag.test/documents/import-from-html"): _FakeResponse(200, body),
    })
    with patch("httpx.Client", return_value=fake):
        ct = _import_under_test()
        out = ct.call_ingest_url({"url": "https://sun.com/policy.html"})
    assert out["success"] is True
    assert out["document_id"] == "abc-123"
    assert "search_corpus next" in out["result"]
    assert "Some Policy" in out["result"]


def test_ingest_url_409_treated_as_success(monkeypatch):
    """rag returns 409 when the URL was already imported — chat should
    treat that as success (the doc IS in the corpus, just from earlier).
    """
    _set_rag_url(monkeypatch)
    fake = _FakeClient({
        ("POST", "https://rag.test/documents/import-from-html"): _FakeResponse(
            409, {"detail": {"error": "duplicate_html", "document_id": "existing-id"}},
        ),
    })
    with patch("httpx.Client", return_value=fake):
        ct = _import_under_test()
        out = ct.call_ingest_url({"url": "https://sun.com/already.html"})
    assert out["success"] is True
    assert out["document_id"] == "existing-id"
    assert "already in the corpus" in out["result"]


def test_ingest_url_5xx_returns_failure(monkeypatch):
    _set_rag_url(monkeypatch)
    fake = _FakeClient({
        ("POST", "https://rag.test/documents/import-from-html"): _FakeResponse(500, text="boom"),
    })
    with patch("httpx.Client", return_value=fake):
        ct = _import_under_test()
        out = ct.call_ingest_url({"url": "https://x.com/y"})
    assert out["success"] is False
    assert "HTTP 500" in out["result"]


def test_ingest_url_passes_optional_metadata(monkeypatch):
    """payer/state/program/authority_level/title are all forwarded
    when present in inputs."""
    _set_rag_url(monkeypatch)
    fake = _FakeClient({
        ("POST", "https://rag.test/documents/import-from-html"): _FakeResponse(200, {
            "url": "https://x.com/a", "document_id": "x", "status": "completed", "sections": 1,
        }),
    })
    with patch("httpx.Client", return_value=fake):
        ct = _import_under_test()
        ct.call_ingest_url({
            "url": "https://x.com/a",
            "payer": "Sunshine Health",
            "state": "FL",
            "authority_level": "payer_manual",
            "title": "Manual",
        })
    method, _, body = fake.calls[0]
    assert method == "POST"
    assert body["url"] == "https://x.com/a"
    assert body["payer"] == "Sunshine Health"
    assert body["state"] == "FL"
    assert body["authority_level"] == "payer_manual"
    assert body["title"] == "Manual"
