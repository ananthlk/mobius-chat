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
