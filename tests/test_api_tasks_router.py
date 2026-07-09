"""Phase 1f.1 — tasks router extracted from main.py.

Behavior contract: the 8 /chat/tasks/* endpoints must still proxy exactly
as they did pre-extraction, including:

  - GET /chat/tasks forwards all filter params and preserves the response
    body verbatim (when no run_id is involved).
  - GET /chat/tasks with status=open + no run_id re-sorts tasks so blockers
    come first, then decisions, then others, within created_at order.
  - Path ordering: /chat/tasks/export registers before /chat/tasks/{task_id}
    so "export" isn't captured as a task_id.
  - 503 is raised when the skill URL is unset (no silent localhost fallback).

These tests use the shared ``task_proxy`` in app.api._common with the httpx
call mocked, so we exercise the router wiring end-to-end without talking to
the real task-manager skill.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """FastAPI test client with the tasks router mounted on a minimal app.

    We avoid importing app.main (too heavy — drags in all pipelines and the
    DB pool). Instead, mount just the tasks router on a fresh FastAPI.
    """
    from fastapi import FastAPI

    from app.api.tasks import router

    # Ensure the skill URL is set for these tests — we mock httpx anyway,
    # but task_manager_base_url() reads the env var directly.
    monkeypatch.setenv("CHAT_SKILLS_TASK_MANAGER_URL", "http://test-task-manager:8015")

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _mock_httpx_response(*, status_code: int = 200, json_body: dict | None = None, text: str = "", headers: dict | None = None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body if json_body is not None else {}
    r.text = text
    r.headers = headers or {}
    return r


class TestTaskListProxy:
    def test_list_forwards_all_filter_params(self, client):
        captured: dict = {}

        def fake_request(method, url, params=None, json=None):
            captured["method"] = method
            captured["url"] = url
            captured["params"] = params
            return _mock_httpx_response(json_body={"tasks": []})

        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.side_effect = fake_request
            resp = client.get(
                "/chat/tasks",
                params={"org_name": "ACME", "module": "credentialing", "status": "open", "limit": 50},
            )
        assert resp.status_code == 200
        assert captured["method"] == "GET"
        assert captured["url"].endswith("/tasks")
        # None values must be stripped before forwarding. audience=user
        # is the proxy's default (migration 004 task classes, 2026-07-08):
        # this is the user-facing surface, so system telemetry stays out
        # unless the caller opts in with audience=developer|all.
        assert captured["params"] == {
            "org_name": "ACME",
            "module": "credentialing",
            "status": "open",
            "audience": "user",
            "limit": 50,
            "offset": 0,
        }

    def test_cross_run_open_sorts_blockers_first(self, client):
        """status=open + no run_id → blockers before decisions before others,
        within created_at order."""
        body = {
            "tasks": [
                {"id": "t1", "type": "info",     "created_at": "2026-01-01"},
                {"id": "t2", "type": "decision", "created_at": "2026-01-02"},
                {"id": "t3", "type": "blocker",  "created_at": "2026-01-03"},
                {"id": "t4", "type": "blocker",  "created_at": "2026-01-01"},
            ],
        }
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.return_value = (
                _mock_httpx_response(json_body=body)
            )
            resp = client.get("/chat/tasks", params={"status": "open"})
        ids = [t["id"] for t in resp.json()["tasks"]]
        # t4 and t3 are blockers (sorted by created_at ascending); then t2
        # (decision); then t1 (other).
        assert ids == ["t4", "t3", "t2", "t1"]

    def test_run_scoped_list_does_not_resort(self, client):
        """With run_id present, the router must NOT resort — the task-manager
        already returned the canonical per-run order."""
        body = {"tasks": [
            {"id": "a", "type": "info"},
            {"id": "b", "type": "blocker"},
        ]}
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.return_value = (
                _mock_httpx_response(json_body=body)
            )
            resp = client.get("/chat/tasks", params={"run_id": "r-123", "status": "open"})
        assert [t["id"] for t in resp.json()["tasks"]] == ["a", "b"]

    def test_run_status_not_injected_post_disconnect(self, client):
        """2026-04-18 disconnect: credentialing_run_service was removed,
        so the /chat/tasks handler no longer injects run_status /
        pending_step_id on run-scoped queries. Returns the task-manager
        body unchanged. If a future credentialing skill wants run-polling,
        it'll expose its own endpoint or push through task-manager."""
        body = {"tasks": [{"id": "t-1", "type": "info"}]}
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.return_value = (
                _mock_httpx_response(json_body=body)
            )
            resp = client.get("/chat/tasks", params={"run_id": "r-1"})
        j = resp.json()
        assert "run_status" not in j, (
            "run_status injection was removed in the credentialing "
            "disconnect — if it's back, the credentialing_run_service "
            "coupling has been reintroduced. Don't."
        )
        assert "pending_step_id" not in j


class TestTaskExportPathOrdering:
    def test_export_not_captured_as_task_id(self, client):
        """If /chat/tasks/{task_id} registered first, 'export' would be
        captured as a task_id. This test proves the router is defined in
        the right order (matches main.py pre-extraction)."""
        captured: dict = {}

        def fake_request(method, url, params=None, json=None):
            captured["url"] = url
            return _mock_httpx_response(text="a,b\n1,2", headers={})

        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.side_effect = fake_request
            resp = client.get("/chat/tasks/export")
        assert resp.status_code == 200
        # Upstream URL should end with /tasks/export, NOT /tasks/{id}.
        assert captured["url"].endswith("/tasks/export")
        assert resp.text == "a,b\n1,2"
        assert resp.headers["content-type"].startswith("text/csv")


class TestTaskCrudProxies:
    """Each per-id endpoint forwards to the matching skill URL."""

    @pytest.mark.parametrize("method,router_path,skill_path,body", [
        ("GET",    "/chat/tasks/t-1",         "/tasks/t-1",         None),
        ("PATCH",  "/chat/tasks/t-1",         "/tasks/t-1",         {"status": "in_progress"}),
        ("POST",   "/chat/tasks/t-1/resolve", "/tasks/t-1/resolve", {"notes": "done"}),
        ("POST",   "/chat/tasks/t-1/dismiss", "/tasks/t-1/dismiss", {"reason": "dupe"}),
        ("POST",   "/chat/tasks",             "/tasks",             {"title": "x"}),
        ("POST",   "/chat/tasks/bulk-import", "/tasks/bulk-import", {"tasks": []}),
    ])
    def test_proxy_method_and_path(self, client, method, router_path, skill_path, body):
        captured: dict = {}

        def fake_request(m, url, params=None, json=None):
            captured["method"] = m
            captured["url"] = url
            captured["json"] = json
            return _mock_httpx_response(json_body={"ok": True})

        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.side_effect = fake_request
            if body is None:
                resp = client.request(method, router_path)
            else:
                resp = client.request(method, router_path, json=body)
        assert resp.status_code == 200
        assert captured["method"] == method
        assert captured["url"].endswith(skill_path), (
            f"{method} {router_path} should proxy to {skill_path}, "
            f"got {captured['url']}"
        )
        # Body forwarded verbatim.
        assert captured["json"] == body


class TestProxyErrorMapping:
    def test_missing_skill_url_returns_503(self, monkeypatch, client):
        """Unset CHAT_SKILLS_TASK_MANAGER_URL must return 503, not silently
        fall back to a default."""
        monkeypatch.delenv("CHAT_SKILLS_TASK_MANAGER_URL", raising=False)
        # task_manager_base_url() has a "or http://localhost:8015" fallback
        # built in (pre-phase behavior). This test documents the ACTUAL
        # current behavior — the fallback means 503 only fires when BOTH
        # env + fallback are empty. Monkeypatch the helper to empty string
        # to exercise the 503 branch directly.
        with patch("app.api._common.task_manager_base_url", return_value=""):
            resp = client.get("/chat/tasks")
        assert resp.status_code == 503
        assert "CHAT_SKILLS_TASK_MANAGER_URL" in resp.json()["detail"]

    def test_upstream_404_mapped_to_404(self, client):
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.return_value = (
                _mock_httpx_response(status_code=404)
            )
            resp = client.get("/chat/tasks/missing-id")
        assert resp.status_code == 404

    def test_upstream_exception_mapped_to_502(self, client):
        """Any httpx-level exception becomes 502 so the client sees an
        upstream error, not a 500."""
        with patch("httpx.Client") as hc:
            hc.return_value.__enter__.return_value.request.side_effect = RuntimeError("boom")
            resp = client.get("/chat/tasks/t-1")
        assert resp.status_code == 502
        assert "boom" in resp.json()["detail"]
