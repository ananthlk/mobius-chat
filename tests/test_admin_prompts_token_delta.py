"""Governor seat, 2026-09-10: the token_counts backfill surfaced
react.critical_rules growing 1402->3093 tokens (2.2x) across 6 versions,
monotonically, found only because someone happened to look at the history
after the fact. create_block_version now computes and surfaces the delta
against the version being superseded at publish time -- "nobody can merge
a version that adds 270 tokens without someone seeing 270."

Verified for real against the dev DB before this test was written
(two live HTTP calls through app.main, publishing a real throwaway block
key and reading the actual delta back) -- this test locks in that
verified behavior with a mocked DB so it doesn't depend on live
credentials or the dev database being reachable.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.admin_prompts import NewBlockVersionRequest, create_block_version


class _FakeTxn:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """fetchval/fetchrow scripted per call, matching the query order
    create_block_version actually issues: prev_row lookup, next_ver
    lookup, then the INSERT ... RETURNING."""

    def __init__(self, prev_row, next_ver, inserted_row):
        self._prev_row = prev_row
        self._next_ver = next_ver
        self._inserted_row = inserted_row
        self.calls: list[str] = []

    def transaction(self):
        return _FakeTxn()

    async def fetchrow(self, sql, *args):
        if "FROM prompt_blocks" in sql and "WHERE block_key = $1 AND active" in sql:
            self.calls.append("prev_row")
            return self._prev_row
        self.calls.append("insert")
        return self._inserted_row

    async def fetchval(self, sql, *args):
        self.calls.append("next_ver")
        return self._next_ver


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


def _row(block_key, version, active, token_counts_json):
    return {"id": 1, "block_key": block_key, "version": version, "active": active,
            "token_counts": token_counts_json}


@pytest.mark.asyncio
async def test_first_version_has_no_delta():
    conn = _FakeConn(prev_row=None, next_ver=1,
                      inserted_row=_row("k", 1, True, '{"gemini": 100}'))
    with patch("app.api.admin_prompts._gate"), \
         patch("app.api.admin_prompts._pool", AsyncMock(return_value=_FakePool(conn))), \
         patch("app.services.prompt_token_counting.compute_token_counts", return_value={"gemini": 100}):
        result = await create_block_version(
            "k", NewBlockVersionRequest(template_body="x", owner="o", created_by="c"),
        )
    assert result["token_delta"] == {}


@pytest.mark.asyncio
async def test_growth_produces_positive_delta_and_pct():
    conn = _FakeConn(
        prev_row=_row("k", 1, True, '{"gemini": 1000}'),
        next_ver=2,
        inserted_row=_row("k", 2, True, '{"gemini": 1270}'),
    )
    with patch("app.api.admin_prompts._gate"), \
         patch("app.api.admin_prompts._pool", AsyncMock(return_value=_FakePool(conn))), \
         patch("app.services.prompt_token_counting.compute_token_counts", return_value={"gemini": 1270}):
        result = await create_block_version(
            "k", NewBlockVersionRequest(template_body="x" * 5000, owner="o", created_by="c"),
        )
    assert result["token_delta"] == {"gemini": {"from": 1000, "to": 1270, "delta": 270, "pct": 27.0}}


@pytest.mark.asyncio
async def test_shrinkage_produces_negative_delta():
    conn = _FakeConn(
        prev_row=_row("k", 1, True, '{"gemini": 1000}'),
        next_ver=2,
        inserted_row=_row("k", 2, True, '{"gemini": 400}'),
    )
    with patch("app.api.admin_prompts._gate"), \
         patch("app.api.admin_prompts._pool", AsyncMock(return_value=_FakePool(conn))), \
         patch("app.services.prompt_token_counting.compute_token_counts", return_value={"gemini": 400}):
        result = await create_block_version(
            "k", NewBlockVersionRequest(template_body="x", owner="o", created_by="c"),
        )
    assert result["token_delta"]["gemini"]["delta"] == -600
    assert result["token_delta"]["gemini"]["pct"] == -60.0


@pytest.mark.asyncio
async def test_no_delta_when_previous_tokenizer_count_was_absent():
    # Previous version never got a gemini count (e.g. tokenizer was down at
    # its publish time) -- must NOT fabricate a delta from a missing count.
    conn = _FakeConn(
        prev_row=_row("k", 1, True, '{}'),
        next_ver=2,
        inserted_row=_row("k", 2, True, '{"gemini": 500}'),
    )
    with patch("app.api.admin_prompts._gate"), \
         patch("app.api.admin_prompts._pool", AsyncMock(return_value=_FakePool(conn))), \
         patch("app.services.prompt_token_counting.compute_token_counts", return_value={"gemini": 500}):
        result = await create_block_version(
            "k", NewBlockVersionRequest(template_body="x", owner="o", created_by="c"),
        )
    assert result["token_delta"] == {}


@pytest.mark.asyncio
async def test_derived_block_kind_never_computes_or_deltas():
    conn = _FakeConn(
        prev_row=_row("k", 1, True, '{}'),
        next_ver=2,
        inserted_row=_row("k", 2, True, '{}'),
    )
    fake_compute = MagicMock(return_value={"gemini": 999})
    with patch("app.api.admin_prompts._gate"), \
         patch("app.api.admin_prompts._pool", AsyncMock(return_value=_FakePool(conn))), \
         patch("app.services.prompt_token_counting.compute_token_counts", fake_compute):
        result = await create_block_version(
            "k", NewBlockVersionRequest(
                template_body="{{ tool_manifest_text }}", block_kind="derived",
                owner="o", created_by="c",
            ),
        )
    fake_compute.assert_not_called()
    assert result["token_delta"] == {}
