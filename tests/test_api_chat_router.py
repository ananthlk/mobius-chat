"""Phase 2b.2 — chat lifecycle router extracted from main.py.

Behavior contract: after extraction the four lifecycle endpoints
must behave identically to the pre-extraction inline code.

Routes under test:
    POST /chat                            — enqueue; returns cid + thread_id
    GET  /chat/response/{correlation_id}  — poll; enriches from DB when done
    GET  /chat/plan/{correlation_id}      — stored plan or 404

``GET /chat/stream/{correlation_id}`` is tested at a smoke level only
(it's an infinite SSE generator — exercising the full stream needs a
real queue + worker fixture). The important invariant for stream is
"the route exists and returns a StreamingResponse" — which route
introspection covers.

What's locked in:

1. **POST /chat queue publish.** Request body flows through the
   ChatRequest model, correlation_id + thread_id come back, the queue
   receives a publish call with the right payload shape.
2. **Auth is wired on POST /chat.** Phase 2d guard — CHAT_AUTH_MODE=
   required without a JWT returns 401. The route-introspection test
   in test_phase_2d_write_endpoint_auth.py already covers this, but
   the behavioral check here catches the specific case where the
   extraction accidentally dropped the Depends(require_user).
3. **GET /chat/response enriches completed responses.** The
   qc_audit + technical_feedback overlay still fires — the extraction
   preserved the helper.
4. **GET /chat/response falls through to DB progress when queue is
   Redis.** The in-memory branch was the pre-extraction default; the
   DB fallback is specifically for the Redis-queue case.
5. **GET /chat/plan returns 404 for missing plans.** Not an empty
   dict — the UI distinguishes those two states.

Test isolation: these tests mount only the chat router, so we don't
drag in the full main.py (worker startup, middleware, static mount
etc.). That's itself a property of the extraction worth asserting.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """Minimal FastAPI app with just the chat router mounted."""
    from app.api.chat import router

    monkeypatch.setenv("CHAT_AUTH_MODE", "off")  # dev default — auth is a no-op

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── POST /chat ───────────────────────────────────────────────────────


class TestPostChat:
    def test_enqueue_returns_correlation_and_thread_ids(self, client):
        """Happy path: body accepted, request enqueued, correlation_id +
        thread_id come back. The enqueue call is mocked so we don't need
        a running queue."""
        captured: dict = {}

        def fake_publish(cid, payload):
            captured["cid"] = cid
            captured["payload"] = payload

        fake_queue = MagicMock()
        fake_queue.publish_request.side_effect = fake_publish
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.ensure_thread", return_value="thread-xyz"):
            resp = client.post("/chat", json={"message": "hi"})
        assert resp.status_code == 200
        body = resp.json()
        # correlation_id is a UUID; don't match exact value, just shape
        assert isinstance(body["correlation_id"], str) and len(body["correlation_id"]) >= 32
        assert body["thread_id"] == "thread-xyz"
        # Payload shape forwarded to queue matches pre-extraction:
        assert captured["payload"]["message"] == "hi"
        assert captured["payload"]["thread_id"] == "thread-xyz"

    def test_use_react_forwarded_when_set(self, client):
        captured: dict = {}

        def fake_publish(cid, payload):
            captured["payload"] = payload

        fake_queue = MagicMock()
        fake_queue.publish_request.side_effect = fake_publish
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.ensure_thread", return_value="t"):
            client.post("/chat", json={"message": "x", "use_react": False})
        assert captured["payload"]["use_react"] is False

    def test_use_react_omitted_when_none(self, client):
        """If the client doesn't send use_react, the worker uses env
        default. The payload must NOT contain use_react=null — worker
        code checks ``"use_react" in payload`` to decide override vs.
        fall-through. Including None would break that."""
        captured: dict = {}

        def fake_publish(cid, payload):
            captured["payload"] = payload

        fake_queue = MagicMock()
        fake_queue.publish_request.side_effect = fake_publish
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.ensure_thread", return_value="t"):
            client.post("/chat", json={"message": "x"})
        assert "use_react" not in captured["payload"]

    def test_chat_mode_forwarded(self, client):
        captured: dict = {}

        def fake_publish(cid, payload):
            captured["payload"] = payload

        fake_queue = MagicMock()
        fake_queue.publish_request.side_effect = fake_publish
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.ensure_thread", return_value="t"):
            client.post("/chat", json={"message": "x", "chat_mode": "agentic"})
        assert captured["payload"]["chat_mode"] == "agentic"

    def test_unknown_chat_mode_422(self, client):
        """ChatRequest uses Literal["copilot", "agentic", "quick"] — a
        typo'd value should 422, not silently coerce. Pydantic enforces
        this; the extraction preserved the constraint."""
        resp = client.post("/chat", json={"message": "x", "chat_mode": "hurried"})
        assert resp.status_code == 422

    def test_extra_fields_ignored(self, client):
        """ChatRequest.model_config sets extra='ignore'. A browser tab
        from a pre-disconnect build that still sends
        credentialing_options=null / reconciliation_upload_id=null must
        get 200, not 422."""
        fake_queue = MagicMock()
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.ensure_thread", return_value="t"):
            resp = client.post("/chat", json={
                "message": "x",
                "credentialing_options": None,          # stale FE field
                "reconciliation_upload_id": None,       # stale FE field
                "totally_unknown": "whatever",
            })
        assert resp.status_code == 200


class TestPostChatAuth:
    """Mirrors test_phase_2d_write_endpoint_auth.py but locally for the
    extracted router. The extraction kept Depends(require_user); if it's
    ever dropped this catches it without needing the full main.py."""

    def test_required_mode_without_jwt_is_401(self, monkeypatch):
        from app.api.chat import router

        monkeypatch.setenv("CHAT_AUTH_MODE", "required")
        app = FastAPI()
        app.include_router(router)
        cli = TestClient(app)
        resp = cli.post("/chat", json={"message": "hi"})
        assert resp.status_code == 401


# ── GET /chat/response/{correlation_id} ──────────────────────────────


class TestGetChatResponse:
    def test_completed_queue_response_is_enriched(self, client):
        """Completed response pulled from the queue should have qc_audit
        + technical_feedback overlaid from Postgres. The helper does
        the lookup — we mock the storage fns to prove the path fires."""
        fake_queue = MagicMock()
        fake_queue.get_response.return_value = {
            "status": "completed",
            "correlation_id": "cid-123",
            "final_message": "done",
        }
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.fetch_turn_qc_audit", return_value={"passed": True, "source": "eval"}), \
             patch("app.api.chat.get_llm_performance_feedback", return_value=None), \
             patch("app.api.chat.get_adjudication_feedback", return_value={"rating": "up"}):
            resp = client.get("/chat/response/cid-123")
        body = resp.json()
        assert resp.status_code == 200
        # qc_audit was added from DB:
        assert body["qc_audit"]["passed"] is True
        # technical_feedback was added from DB:
        assert body["technical_feedback"]["adjudication"]["rating"] == "up"

    def test_in_progress_returns_processing(self, client):
        """No response yet but progress is being made — the shape
        should carry thinking_log + partial message. Tests that the
        in-memory progress branch fires when queue_type != redis."""
        fake_queue = MagicMock()
        fake_queue.get_response.return_value = None
        fake_cfg = MagicMock()
        fake_cfg.queue_type = "memory"
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.get_response", return_value=None), \
             patch("app.api.chat.get_config", return_value=fake_cfg), \
             patch("app.api.chat.get_progress", return_value=(True, ["step 1", "step 2"], "partial answer...")):
            resp = client.get("/chat/response/cid-123")
        body = resp.json()
        assert body["status"] == "processing"
        assert body["message"] == "partial answer..."
        assert body["thinking_log"] == ["step 1", "step 2"]

    def test_redis_queue_falls_through_to_db(self, client):
        """When the worker is separate (Redis queue), in-memory progress
        is empty in the API process — the DB fallback must fire.
        Regression guard: if the if-branch on queue_type ever flips,
        live progress disappears silently in prod."""
        fake_queue = MagicMock()
        fake_queue.get_response.return_value = None
        fake_cfg = MagicMock()
        fake_cfg.queue_type = "redis"
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.get_response", return_value=None), \
             patch("app.api.chat.get_config", return_value=fake_cfg), \
             patch("app.api.chat.get_progress", return_value=(False, [], "")), \
             patch("app.api.chat.get_progress_from_db", return_value=(["db-step"], "db-partial")) as db_mock:
            resp = client.get("/chat/response/cid-123")
        body = resp.json()
        assert body["status"] == "processing"
        assert body["message"] == "db-partial"
        assert body["thinking_log"] == ["db-step"]
        db_mock.assert_called_once_with("cid-123")

    def test_not_found_returns_pending(self, client):
        """No response, no progress → 'pending' with nulls. Pre-
        extraction behavior; the UI polls until it flips to processing
        or completed."""
        fake_queue = MagicMock()
        fake_queue.get_response.return_value = None
        fake_cfg = MagicMock()
        fake_cfg.queue_type = "memory"
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.get_response", return_value=None), \
             patch("app.api.chat.get_config", return_value=fake_cfg), \
             patch("app.api.chat.get_progress", return_value=(False, [], "")):
            resp = client.get("/chat/response/missing-cid")
        body = resp.json()
        assert body["status"] == "pending"
        assert body["message"] is None
        assert body["thinking_log"] is None


class TestEnrichHelper:
    """Locks the _enrich_completed_response_from_db contract. The UI
    polls these fields after the user submits feedback — if the
    overlay doesn't happen, the browser shows 'no feedback yet' even
    after it was submitted."""

    def test_non_completed_response_passes_through_unchanged(self):
        from app.api.chat import _enrich_completed_response_from_db

        resp = {"status": "processing", "correlation_id": "x"}
        out = _enrich_completed_response_from_db(resp)
        assert out is resp  # no overlay, return as-is

    def test_missing_correlation_id_passes_through(self):
        from app.api.chat import _enrich_completed_response_from_db

        resp = {"status": "completed"}  # no correlation_id
        out = _enrich_completed_response_from_db(resp)
        assert out is resp

    def test_db_errors_swallowed_as_debug_log(self):
        """DB hiccup shouldn't fail the whole response — the overlay
        is best-effort. If this ever starts raising, every GET /chat/
        response fails when the feedback DB is down."""
        from app.api.chat import _enrich_completed_response_from_db

        resp = {"status": "completed", "correlation_id": "cid"}
        with patch("app.api.chat.fetch_turn_qc_audit", side_effect=RuntimeError("db down")):
            out = _enrich_completed_response_from_db(resp)
        assert out["correlation_id"] == "cid"  # still returned
        # No qc_audit added (fetch raised), and we didn't crash.
        assert "qc_audit" not in out


# ── GET /chat/plan/{correlation_id} ──────────────────────────────────


class TestGetChatPlan:
    def test_found_plan_is_returned(self, client):
        plan_payload = {"subquestions": [{"id": "sq1", "text": "what's PA?"}]}
        with patch("app.api.chat.get_plan", return_value=plan_payload):
            resp = client.get("/chat/plan/cid-123")
        assert resp.status_code == 200
        assert resp.json() == plan_payload

    def test_missing_plan_is_404(self, client):
        """404 not 200-with-empty — UI shows a different state for
        'never existed' vs. 'empty plan.' Regression-guard comment in
        the endpoint docstring, locked here."""
        with patch("app.api.chat.get_plan", return_value=None):
            resp = client.get("/chat/plan/missing-cid")
        assert resp.status_code == 404
        assert "Plan not found" in resp.json()["detail"]


# ── Route presence (smoke) ───────────────────────────────────────────


class TestRoutesMounted:
    def test_stream_endpoint_exists(self, client):
        """chat_stream is an SSE generator — we don't exercise the full
        stream here. Just assert the route is mounted by checking that
        a GET with a bogus cid doesn't 404. Using stream=True so the
        TestClient doesn't block waiting for the stream to close."""
        fake_queue = MagicMock()
        fake_queue.get_response.return_value = {"status": "completed", "correlation_id": "c"}
        fake_cfg = MagicMock()
        fake_cfg.queue_type = "memory"
        with patch("app.api.chat.get_queue", return_value=fake_queue), \
             patch("app.api.chat.get_response", return_value=None), \
             patch("app.api.chat.get_config", return_value=fake_cfg):
            with client.stream("GET", "/chat/stream/c") as resp:
                # Route exists and returns an SSE-shaped response.
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                # Read one chunk then close — don't drain the stream.
                for _ in resp.iter_lines():
                    break
