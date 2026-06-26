"""_acquire_conn one-shot fallback — the bandit-telemetry keystone fix.

Previously every generate_sync-from-async caller (integrator, react,
planner, thread_summary) silently dropped its llm_calls row: generate_sync
runs generate() in a throwaway thread/loop, pg_pool.get_pool() returns None
for that foreign loop, and the writers no-op'd on None. _acquire_conn now
falls back to a one-shot connection so the write lands.
"""
from __future__ import annotations

import asyncio

import app.services.llm_analytics as la


def test_acquire_conn_uses_pool_when_available(monkeypatch):
    """Main-loop path: the shared pool is used; no one-shot connect."""
    acquired = {"v": False}

    class _PoolConn:
        async def execute(self, *a, **k):
            return "OK"

    class _AcquireCtx:
        async def __aenter__(self):
            acquired["v"] = True
            return _PoolConn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _AcquireCtx()

    async def _get_pool():
        return _Pool()

    monkeypatch.setattr("app.services.pg_pool.get_pool", _get_pool)

    async def run():
        async with la._acquire_conn() as conn:
            assert conn is not None
            await conn.execute("SELECT 1")

    asyncio.run(run())
    assert acquired["v"] is True


def test_acquire_conn_one_shot_fallback_on_foreign_loop(monkeypatch):
    """Foreign loop (pool None) + DSN present → transient connection that is
    opened and then closed."""
    closed = {"v": False}

    class _FakeConn:
        async def execute(self, *a, **k):
            return "OK"

        async def close(self):
            closed["v"] = True

    async def _no_pool():
        return None

    async def _connect(url, **k):
        return _FakeConn()

    monkeypatch.setattr("app.services.pg_pool.get_pool", _no_pool)
    monkeypatch.setattr("app.db_client._get_fallback_url", lambda which: "postgresql://u@/db")
    import asyncpg

    monkeypatch.setattr(asyncpg, "connect", _connect)

    async def run():
        async with la._acquire_conn() as conn:
            assert conn is not None
            await conn.execute("INSERT ...")

    asyncio.run(run())
    assert closed["v"] is True  # one-shot connection was closed


def test_acquire_conn_yields_none_without_dsn(monkeypatch):
    """No pool and no DSN → yields None; callers skip."""
    async def _no_pool():
        return None

    monkeypatch.setattr("app.services.pg_pool.get_pool", _no_pool)
    monkeypatch.setattr("app.db_client._get_fallback_url", lambda which: "")

    async def run():
        async with la._acquire_conn() as conn:
            return conn

    assert asyncio.run(run()) is None


def test_write_async_lands_via_one_shot(monkeypatch):
    """End-to-end of the fix: _write_async on a foreign loop reaches the
    one-shot connection's execute() instead of no-op'ing."""
    executed = {"v": False}

    class _FakeConn:
        async def execute(self, *a, **k):
            executed["v"] = True

        async def close(self):
            pass

    async def _no_pool():
        return None

    async def _connect(url, **k):
        return _FakeConn()

    monkeypatch.setattr("app.services.pg_pool.get_pool", _no_pool)
    monkeypatch.setattr("app.db_client._get_fallback_url", lambda which: "postgresql://u@/db")
    import asyncpg

    monkeypatch.setattr(asyncpg, "connect", _connect)

    from datetime import datetime, timezone

    rec = {k: None for k in (
        "call_id", "correlation_id", "thread_id", "config_sha", "model", "provider",
        "stage", "tier", "complexity", "is_ab_call", "ab_variant", "success",
        "is_rate_limit", "is_fallback", "fallback_from", "completion_valid", "error_type",
        "latency_ms", "input_tokens", "output_tokens", "cost_usd", "quality_score",
        "quality_source", "phi_detected", "phi_scrubbed", "phi_types", "prompt_len_chars",
        "output_len_chars", "prompt_hash", "synced_to_bq", "synced_at",
    )}
    rec["ts"] = datetime.now(timezone.utc)
    rec["stage"] = "thread_summary"

    asyncio.run(la._write_async(rec))
    assert executed["v"] is True
