"""Phase 2b.1 — doc-reader router extracted from main.py.

Behavior contract: after extraction, the four ``/chat/doc-reader/*``
endpoints must behave identically to the pre-extraction inline code.
Specifically:

  - URL paths unchanged (critical — FE + MCP consumers hit the old
    paths; a regression here breaks every doc-reader integration at
    once).
  - POST endpoints still require auth in ``CHAT_AUTH_MODE=required``
    (Phase 2d protection preserved across the refactor).
  - ``/health`` remains unauthenticated so monitoring works.
  - Upstream errors (connection refused, 5xx, non-JSON) map to 502 on
    our side — clients get a consistent error shape regardless of
    what the upstream skill is doing.
  - ``CHAT_SKILLS_DOC_READER_URL`` env var is the toggle (falls back
    to ``http://localhost:8018``), and the resolution happens at
    request time (not module load) so tests can monkeypatch.

These tests use the shared ``_doc_reader_proxy`` helper with httpx
mocked, so the router wiring is exercised end-to-end without actually
talking to the doc-reader skill.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── Test fixture: minimal app with just the doc-reader router ─────────


@pytest.fixture
def client(monkeypatch):
    """Mount just the doc_reader router on a fresh FastAPI app.

    Avoids importing app.main (too heavy — drags in the pipeline
    stack, worker startup, etc.). The router is self-contained,
    which is itself a property of the extraction worth asserting.
    """
    from app.api.doc_reader import router

    # Known upstream URL so 502-mapping tests have a stable base.
    monkeypatch.setenv("CHAT_SKILLS_DOC_READER_URL", "http://test-doc-reader:8018")
    # Dev-mode auth so the POST endpoints are reachable without a JWT.
    # Hosted-mode auth is tested separately in
    # test_phase_2d_write_endpoint_auth.py.
    monkeypatch.setenv("CHAT_AUTH_MODE", "off")

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _mock_httpx_response(*, status_code: int = 200, json_body: dict | None = None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body if json_body is not None else {}
    # Match httpx.Response contract: raise_for_status is a no-op on 2xx,
    # raises for 4xx/5xx. The proxy helper catches any Exception and
    # maps to 502, so the exact subclass doesn't matter for these tests.
    if status_code >= 400:
        r.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        r.raise_for_status.return_value = None
    return r


# ── Route presence: paths + methods unchanged after extraction ────────


class TestRouterPaths:
    def test_all_four_routes_mounted(self, client):
        """If main.py's inclusion regresses or the router omits a route,
        these 404s surface here — not in production when a FE calls
        /chat/doc-reader/extract and gets a 404 instead of the extraction
        response it expected."""
        # A mocked-upstream call proves the route is wired AND that the
        # proxy helper runs. HEAD-style existence checks don't exercise
        # the helper.
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.return_value = (
                _mock_httpx_response(json_body={"ok": True})
            )
            assert client.post("/chat/doc-reader/read", json={}).status_code == 200
            assert client.post("/chat/doc-reader/extract", json={}).status_code == 200
            assert client.post("/chat/doc-reader/summarize", json={}).status_code == 200
            assert client.get("/chat/doc-reader/health").status_code == 200


class TestProxyForwarding:
    def test_read_forwards_body_and_path(self, client):
        captured: dict = {}

        def fake_request(method, url, json=None, params=None):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = json
            return _mock_httpx_response(json_body={"pages": []})

        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.side_effect = fake_request
            body = {"document_id": "doc-123", "mode": "full"}
            resp = client.post("/chat/doc-reader/read", json=body)
        assert resp.status_code == 200
        assert resp.json() == {"pages": []}
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/read"), (
            f"Expected upstream path /read, got {captured['url']}"
        )
        assert captured["url"].startswith("http://test-doc-reader:8018")
        assert captured["json"] == body

    def test_extract_uses_longer_timeout(self, client):
        """Extraction is retrieval + re-rank upstream — the helper
        passes timeout=60. If the refactor dropped the argument,
        long extractions would start timing out at 30s."""
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.return_value = (
                _mock_httpx_response(json_body={"hits": []})
            )
            client.post("/chat/doc-reader/extract", json={"document_id": "d", "query": "?"})
            # httpx.Client(...) was called with timeout=60.0 when the
            # /extract route fired.
            call_kwargs = hc.call_args.kwargs
            assert call_kwargs.get("timeout") == 60.0

    def test_summarize_uses_longer_timeout(self, client):
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.return_value = (
                _mock_httpx_response(json_body={"summary": ""})
            )
            client.post("/chat/doc-reader/summarize", json={"document_id": "d"})
            assert hc.call_args.kwargs.get("timeout") == 60.0

    def test_read_uses_default_timeout(self, client):
        """Read uses the helper's default 30s — no custom timeout was
        passed in the original main.py code and shouldn't be added in
        extraction."""
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.return_value = (
                _mock_httpx_response(json_body={"ok": True})
            )
            client.post("/chat/doc-reader/read", json={})
            assert hc.call_args.kwargs.get("timeout") == 30.0

    def test_health_is_get_not_post(self, client):
        """The four-route extraction keeps /health as GET (only the
        three content routes are POST). If a refactor accidentally
        renamed it to POST, readiness probes break."""
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.return_value = (
                _mock_httpx_response(json_body={"status": "ok"})
            )
            resp = client.get("/chat/doc-reader/health")
        assert resp.status_code == 200


# ── Error mapping ─────────────────────────────────────────────────────


class TestErrorMapping:
    def test_upstream_5xx_becomes_502(self, client):
        """Doc-reader returned a 500? Chat maps to 502 so the client
        can distinguish 'chat is up, upstream isn't' from 'chat itself
        is broken.'"""
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.return_value = (
                _mock_httpx_response(status_code=500)
            )
            resp = client.post("/chat/doc-reader/read", json={})
        assert resp.status_code == 502
        assert "Doc-reader skill error" in resp.json()["detail"]

    def test_upstream_connection_error_becomes_502(self, client):
        """Doc-reader unreachable (connection refused) → 502. Same
        behavior as 5xx — client can't tell the difference and
        shouldn't need to."""
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.side_effect = (
                ConnectionError("doc-reader is down")
            )
            resp = client.post("/chat/doc-reader/extract", json={})
        assert resp.status_code == 502
        assert "doc-reader is down" in resp.json()["detail"]

    def test_upstream_returns_non_json_becomes_502(self, client):
        """If the upstream returns HTML (e.g. a proxy error page) or
        broken JSON, the .json() call raises — which the proxy maps
        to 502, not a 500 with a JSONDecodeError traceback."""
        r = MagicMock()
        r.status_code = 200
        r.raise_for_status.return_value = None
        r.json.side_effect = ValueError("not json")
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.return_value = r
            resp = client.post("/chat/doc-reader/summarize", json={})
        assert resp.status_code == 502


# ── Config resolution ─────────────────────────────────────────────────


class TestUpstreamUrlResolution:
    def test_resolves_from_env(self, monkeypatch):
        """_doc_reader_base_url reads the env at call time (not module
        load) so tests — and operators running the container — can
        change the URL without a restart for this helper specifically.
        A startup-time read would cache the value and tests would need
        reimport voodoo to change it."""
        from app.api.doc_reader import _doc_reader_base_url

        monkeypatch.setenv("CHAT_SKILLS_DOC_READER_URL", "https://prod-dr.example.com/")
        assert _doc_reader_base_url() == "https://prod-dr.example.com"

    def test_falls_back_to_localhost(self, monkeypatch):
        monkeypatch.delenv("CHAT_SKILLS_DOC_READER_URL", raising=False)
        from app.api.doc_reader import _doc_reader_base_url

        # Matches the default that the pre-extraction inline code used.
        # Worth locking so a well-meaning cleanup of the localhost
        # fallback doesn't silently break dev envs that rely on it.
        assert _doc_reader_base_url() == "http://localhost:8018"

    def test_strips_trailing_slash(self, monkeypatch):
        """Trailing slash would produce URLs like ``.../read`` →
        ``.../read/`` after join. Some upstreams 404 on that. Strip
        at resolution time, not at every call site."""
        from app.api.doc_reader import _doc_reader_base_url

        monkeypatch.setenv("CHAT_SKILLS_DOC_READER_URL", "http://x.example.com:8018///")
        assert not _doc_reader_base_url().endswith("/")
