"""Unit tests for LLM analytics: build_record and write_record."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services import llm_analytics
from app.services.usage import usage_dict


def test_build_record_shape_and_prompt_hash():
    """build_record returns dict with call_id, ts, prompt_hash; no raw prompt."""
    record = llm_analytics.build_record(
        model="gemini-2.5-flash",
        provider="vertex",
        stage="planner",
        success=True,
        prompt="Hello world",
        output_text="Hi",
        usage=usage_dict("vertex", "gemini-2.5-flash", 10, 5),
        latency_ms=100,
        config_sha="abc",
        correlation_id="cid",
        thread_id="tid",
    )
    assert "call_id" in record
    assert "ts" in record
    assert record["model"] == "gemini-2.5-flash"
    assert record["provider"] == "vertex"
    assert record["stage"] == "planner"
    assert record["success"] is True
    assert record["prompt_hash"] == llm_analytics._hash_prompt("Hello world")
    assert "Hello world" not in str(record)
    assert record["input_tokens"] == 10
    assert record["output_tokens"] == 5
    assert record["latency_ms"] == 100
    assert record["config_sha"] == "abc"
    assert record["correlation_id"] == "cid"
    assert record["thread_id"] == "tid"
    assert record["cost_usd"] is not None
    assert record["prompt_len_chars"] == 11
    assert record["output_len_chars"] == 2


def test_build_record_module_key_mirrors_stage():
    """module_key ≡ stage is the ratified 1:1 rule (migration 051, DQ-2).

    Regression test: build_record never populated module_key/variant_id
    (only migration 051's one-time backfill UPDATE ever set them, for
    rows that existed before it ran) — every row inserted since has had
    both NULL, which silently zeroed out model_performance_by_stage's
    `WHERE variant_id = 'default'` filter (migration 052) for over a
    month. See the comment in build_record() for the full mechanism.
    """
    record = llm_analytics.build_record(
        model="gemini-2.5-flash",
        provider="vertex",
        stage="adjudicator",
        success=True,
        prompt="hi",
    )
    assert record["module_key"] == "adjudicator"
    assert record["variant_id"] == "default"


def test_build_record_module_key_uses_normalized_stage():
    """module_key mirrors the SAME normalized stage value written to
    `stage` (falls back to "unknown"), not the raw unnormalized input."""
    record = llm_analytics.build_record(
        model="x", provider="y", stage="  ", success=True, prompt="",
    )
    assert record["stage"] == "unknown"
    assert record["module_key"] == "unknown"


def test_build_record_minimal():
    """build_record with minimal args uses defaults."""
    record = llm_analytics.build_record(
        model="x",
        provider="y",
        stage="z",
        success=False,
        prompt="",
    )
    assert record["model"] == "x"
    assert record["success"] is False
    assert record["input_tokens"] is None
    assert record["output_tokens"] is None
    assert record["cost_usd"] is None
    assert record["prompt_hash"] == llm_analytics._hash_prompt("")


def test_write_record_no_crash():
    """write_record does not crash when pool is unavailable (no loop)."""
    record = llm_analytics.build_record(
        model="m",
        provider="p",
        stage="s",
        success=True,
        prompt="p",
    )
    with patch("app.services.llm_analytics.asyncio.get_running_loop") as mock_loop:
        mock_loop.side_effect = RuntimeError("no loop")
        with patch("app.services.llm_analytics._write_async", new_callable=AsyncMock):
            llm_analytics.write_record(record)
    # asyncio.run(_write_async(record)) is called; mock swallows it
    # If we didn't patch _write_async, no pool would be used and we'd just return in _write_async


def test_write_record_fire_and_forget_with_loop():
    """When event loop is running, write_record schedules _write_async as task."""
    record = llm_analytics.build_record(
        model="m",
        provider="p",
        stage="s",
        success=True,
        prompt="p",
    )
    created_task = []

    async def run():
        with patch("app.services.llm_analytics._write_async", new_callable=AsyncMock) as mock_write:
            llm_analytics.write_record(record)
            await asyncio.sleep(0.05)
            mock_write.assert_called_once_with(record)

    asyncio.run(run())


class _FakeConn:
    """Mimics asyncpg.Connection.execute()'s command-tag return shape."""

    def __init__(self, update_tag: str):
        self._update_tag = update_tag
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if sql.strip().startswith("UPDATE"):
            return self._update_tag
        return "INSERT 0 1"


class _FakeAcquireConn:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def test_update_quality_async_returns_true_on_real_write():
    """UPDATE matching 1 row -> True, and the quality_updates INSERT runs."""
    conn = _FakeConn(update_tag="UPDATE 1")
    with patch.object(llm_analytics, "_acquire_conn", return_value=_FakeAcquireConn(conn)):
        result = asyncio.run(
            llm_analytics.update_quality_async("11111111-1111-1111-1111-111111111111", 0.9, "test_source")
        )
    assert result is True
    assert len(conn.executed) == 2  # UPDATE + INSERT both ran


def test_update_quality_async_returns_false_when_update_matches_zero_rows():
    """Task #32: UPDATE matching 0 rows (stale/bad call_id) must return
    False, not silently report success -- this is the exact bug that made
    bandit_reward_persisted's "persisted" gate untrustworthy."""
    conn = _FakeConn(update_tag="UPDATE 0")
    with patch.object(llm_analytics, "_acquire_conn", return_value=_FakeAcquireConn(conn)):
        result = asyncio.run(
            llm_analytics.update_quality_async("11111111-1111-1111-1111-111111111111", 0.9, "test_source")
        )
    assert result is False
    # The quality_updates INSERT must NOT run for a call_id that doesn't exist.
    assert len(conn.executed) == 1


def test_write_async_sends_module_key_and_variant_id():
    """Regression test for the mechanism, not just build_record's dict shape:
    _write_async's INSERT column list and positional args must actually
    include module_key/variant_id, and the column/placeholder/arg counts
    must all agree — a mismatch here is exactly how this went unnoticed
    for a month (asyncpg raises on a count mismatch, but a same-length
    swap or a silently-omitted column raises nothing)."""
    record = llm_analytics.build_record(
        model="gemini-2.5-pro", provider="vertex", stage="adjudicator",
        success=True, prompt="hi",
    )
    conn = _FakeConn(update_tag="INSERT 0 1")
    with patch.object(llm_analytics, "_acquire_conn", return_value=_FakeAcquireConn(conn)):
        asyncio.run(llm_analytics._write_async(record))
    assert len(conn.executed) == 1
    sql, args = conn.executed[0]
    assert "module_key" in sql
    assert "variant_id" in sql
    assert "adjudicator" in args
    assert "default" in args
