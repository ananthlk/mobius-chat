"""Tests for the 2026-04-22 "no-regrets" latency fixes.

Covers:
  - B: Vertex per-call HTTP timeout (``VERTEX_HTTP_TIMEOUT_SECONDS``) is
       passed through to generate_content and falls back cleanly when
       the SDK rejects the kwarg.
  - C: ``_acquire_conn`` / ``_release_conn`` use a ThreadedConnectionPool
       by default and survive pool misses by falling through to direct
       connect.
  - G: ``run_pipeline`` emits an immediate "◌ Thinking…" line before
       state_load runs.
  - D: ``/chat/stream`` SSE response carries the hardened headers
       (``X-Accel-Buffering: no``) and flushes an open comment as the
       first byte.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


# ── Item B: Vertex HTTP timeout ───────────────────────────────────────


def test_vertex_request_options_honors_env(monkeypatch):
    from app.services import llm_provider as lp

    monkeypatch.setenv("VERTEX_HTTP_TIMEOUT_SECONDS", "15")
    opts = lp._vertex_request_options()
    assert opts == {"timeout": 15.0}


def test_vertex_request_options_defaults_to_30(monkeypatch):
    from app.services import llm_provider as lp

    monkeypatch.delenv("VERTEX_HTTP_TIMEOUT_SECONDS", raising=False)
    opts = lp._vertex_request_options()
    assert opts == {"timeout": 30.0}


def test_vertex_request_options_clamps_bad_values(monkeypatch):
    from app.services import llm_provider as lp

    monkeypatch.setenv("VERTEX_HTTP_TIMEOUT_SECONDS", "not-a-number")
    opts = lp._vertex_request_options()
    assert opts == {"timeout": 30.0}


def test_vertex_generate_sync_passes_timeout_kwarg(monkeypatch):
    """generate_content should be called with timeout from request_options."""
    from app.services import llm_provider as lp

    monkeypatch.setenv("VERTEX_HTTP_TIMEOUT_SECONDS", "20")

    fake_model = MagicMock()
    fake_model.generate_content.return_value = MagicMock(text="ok", usage_metadata=None)

    with patch("vertexai.generative_models.GenerativeModel", return_value=fake_model):
        lp._vertex_generate_sync("gemini-2.5-flash", "hi", {"temperature": 0.1})

    # The call must include a timeout kwarg.
    _, kwargs = fake_model.generate_content.call_args
    assert kwargs.get("timeout") == 20.0


def test_vertex_generate_sync_falls_back_when_timeout_kwarg_rejected(monkeypatch):
    """Older SDKs reject the ``timeout`` kwarg with TypeError — we must
    retry without it rather than crash."""
    from app.services import llm_provider as lp

    fake_model = MagicMock()
    fake_response = MagicMock(text="ok", usage_metadata=None)
    fake_model.generate_content.side_effect = [TypeError("unexpected kwarg"), fake_response]

    with patch("vertexai.generative_models.GenerativeModel", return_value=fake_model):
        text, _ = lp._vertex_generate_sync("gemini-2.5-flash", "hi", {"temperature": 0.1})

    assert text == "ok"
    assert fake_model.generate_content.call_count == 2
    # Second call must have NO timeout kwarg.
    _, second_kwargs = fake_model.generate_content.call_args_list[1]
    assert "timeout" not in second_kwargs


# ── Item C: DB connection pool ────────────────────────────────────────


def test_get_pool_max_honors_env_and_clamps(monkeypatch):
    from app import db_client

    monkeypatch.setenv("CHAT_DB_POOL_MAX", "25")
    assert db_client._get_pool_max() == 25

    monkeypatch.setenv("CHAT_DB_POOL_MAX", "0")
    assert db_client._get_pool_max() == 1  # clamped to min 1

    monkeypatch.setenv("CHAT_DB_POOL_MAX", "9999")
    assert db_client._get_pool_max() == 50  # clamped to max 50

    monkeypatch.setenv("CHAT_DB_POOL_MAX", "abc")
    assert db_client._get_pool_max() == 10  # default on bad input


def test_get_pool_returns_none_when_pool_creation_fails(monkeypatch):
    from app import db_client

    # Clear any cached pool for this URL.
    db_client._POOLS.clear()

    class BrokenPool:
        def __init__(self, **_):
            raise RuntimeError("cannot reach db")

    fake_pool_module = MagicMock()
    fake_pool_module.ThreadedConnectionPool = BrokenPool

    with patch.dict("sys.modules", {"psycopg2.pool": fake_pool_module}):
        result = db_client._get_pool("postgresql://fake/db")

    assert result is None


def test_acquire_conn_falls_back_to_direct_connect_when_no_pool(monkeypatch):
    """When the pool is None, _acquire_conn uses a direct psycopg2.connect."""
    from app import db_client

    db_client._POOLS.clear()

    fake_conn = MagicMock()
    with patch.object(db_client, "_get_pool", return_value=None), \
         patch("psycopg2.connect", return_value=fake_conn) as mock_connect:
        conn, is_pooled = db_client._acquire_conn("postgresql://fake/db")

    assert conn is fake_conn
    assert is_pooled is False
    mock_connect.assert_called_once()


def test_release_conn_on_broken_closes_pool_connection():
    from app import db_client

    pool = MagicMock()
    db_client._POOLS["postgresql://fake/db"] = pool
    try:
        conn = MagicMock()
        db_client._release_conn("postgresql://fake/db", conn, is_pooled=True, is_broken=True)
        pool.putconn.assert_called_once_with(conn, close=True)
    finally:
        db_client._POOLS.pop("postgresql://fake/db", None)


def test_release_conn_on_healthy_returns_to_pool():
    from app import db_client

    pool = MagicMock()
    db_client._POOLS["postgresql://fake/db"] = pool
    try:
        conn = MagicMock()
        db_client._release_conn("postgresql://fake/db", conn, is_pooled=True, is_broken=False)
        pool.putconn.assert_called_once_with(conn, close=False)
    finally:
        db_client._POOLS.pop("postgresql://fake/db", None)


def test_release_conn_non_pooled_closes_directly():
    from app import db_client

    conn = MagicMock()
    db_client._release_conn("postgresql://fake/db", conn, is_pooled=False)
    conn.close.assert_called_once()


# ── Item G: Immediate thinking line ───────────────────────────────────


def test_run_pipeline_emits_thinking_before_state_load():
    """The first emitted thinking chunk should arrive BEFORE state_load,
    so the UI sees motion within ~100ms of POST /chat."""
    from app.pipeline import orchestrator

    order: list[str] = []

    # Capture every emit; proves the ◌ line lands first.
    def fake_state_load(ctx):
        order.append("state_load_ran")
        ctx.merged_state = {"active": {}}
        ctx.last_turns = []

    def fake_send_to_user(cid, payload):
        if payload.get("type") == "thinking":
            order.append(("emit", payload.get("content")))

    with patch.object(orchestrator, "run_state_load", side_effect=fake_state_load), \
         patch.object(orchestrator, "send_to_user", side_effect=fake_send_to_user), \
         patch.object(orchestrator, "start_progress"), \
         patch.object(orchestrator, "register_open_slots"), \
         patch.object(orchestrator, "save_state_full"), \
         patch.object(orchestrator, "store_plan"), \
         patch.object(orchestrator, "store_response"), \
         patch.object(orchestrator, "get_persistence"), \
         patch.object(orchestrator, "get_queue"), \
         patch.object(orchestrator, "clear_progress"), \
         patch.object(orchestrator, "run_classify"), \
         patch.object(orchestrator, "run_plan"), \
         patch.object(orchestrator, "run_clarify"), \
         patch.object(orchestrator, "run_resolve"), \
         patch.object(orchestrator, "run_integrate"), \
         patch.object(orchestrator, "_publish_completed"), \
         patch.dict(os.environ, {"MOBIUS_USE_REACT": "0"}):
        try:
            orchestrator.run_pipeline(
                "cid-abc",
                "hello",
                "thread-abc",
                t0_start=0.0,
                use_react_override=False,
            )
        except Exception:
            # We don't care if downstream stages crash in this
            # heavily-mocked test — we only want to verify ordering.
            pass

    # Find the first "emit" — it must happen before state_load_ran.
    first_emit_idx = next(
        (i for i, x in enumerate(order) if isinstance(x, tuple) and x[0] == "emit"),
        -1,
    )
    state_load_idx = order.index("state_load_ran") if "state_load_ran" in order else -1

    assert first_emit_idx >= 0, f"No emit captured: order={order}"
    assert state_load_idx >= 0, f"state_load never ran: order={order}"
    assert first_emit_idx < state_load_idx, (
        f"Expected emit before state_load; got order={order}"
    )
    # The first emit should be the "◌ Thinking…" perception line.
    assert "Thinking" in order[first_emit_idx][1]


# ── Item D: SSE hardening ─────────────────────────────────────────────


def test_chat_stream_sets_x_accel_buffering_header(monkeypatch):
    """Cloud Run / nginx-family proxies must see X-Accel-Buffering: no
    so they don't buffer small SSE chunks.

    We don't actually hit the route here — the SSE generator is an
    infinite poll loop that would hang any unit test without a real
    response in the queue. Instead, we assert against the route handler
    directly: we call it and inspect the ``StreamingResponse`` headers
    without ever iterating the body."""
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from app.api.chat import router as _r  # noqa: F401 — ensure module imports

    # Drive the handler ourselves so the event_generator is never awaited.
    monkeypatch.setenv("CHAT_STREAM_TIMEOUT_S", "1")
    import asyncio
    from app.api.chat import chat_stream

    # chat_stream is async — run just enough to build the StreamingResponse.
    resp = asyncio.run(chat_stream("nonexistent-cid"))
    assert isinstance(resp, StreamingResponse)
    assert resp.headers.get("x-accel-buffering") == "no"
    assert resp.headers.get("cache-control") == "no-cache, no-transform"
    assert resp.headers.get("connection") == "keep-alive"


def test_chat_stream_flushes_open_comment_first(monkeypatch):
    """First bytes on the wire should be the ``: stream-open`` comment
    — forces proxies to flush rather than buffer-until-timeout."""
    import asyncio
    from app.api.chat import chat_stream

    monkeypatch.setenv("CHAT_STREAM_TIMEOUT_S", "1")
    resp = asyncio.run(chat_stream("nonexistent-cid-2"))

    async def _first_chunk():
        async for c in resp.body_iterator:
            return c
        return b""

    first = asyncio.run(_first_chunk())
    if isinstance(first, bytes):
        first = first.decode("utf-8", errors="replace")
    assert ": stream-open" in first
