"""Governor seat, 2026-09-10: admin_prompts.create_block_version() and
react_block_seed.py's seed() were two independent writers to prompt_blocks
-- migration 066 (token_counts) landed in only one. "The next feature
added to one path is the next thing the other forgets." Both now call
publish_block_version() (this module) instead of duplicating the insert
logic. These tests cover the shared function's own contract directly,
independent of either caller.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.prompt_block_publish import publish_block_version


class _FakeConn:
    def __init__(self, existing=None, prev_row=None, insert_result="normal"):
        # existing: truthy if the pre-check SELECT should find a row already there.
        # insert_result: "normal" -> returns a real row; "race" -> None (lost the
        # ON CONFLICT race to a concurrent publisher).
        self._existing = existing
        self._prev_row = prev_row
        self._insert_result = insert_result
        self.fetchval_calls = 0
        self.fetchrow_calls = 0

    async def fetchval(self, sql, *args):
        self.fetchval_calls += 1
        if "SELECT 1 FROM prompt_blocks" in sql:
            return 1 if self._existing else None
        if "COALESCE(MAX(version)" in sql:
            return 5  # next_ver when version=None
        return None

    async def fetchrow(self, sql, *args):
        self.fetchrow_calls += 1
        if "WHERE block_key = $1 AND active = true" in sql:
            return self._prev_row
        # The INSERT ... RETURNING
        if self._insert_result == "race":
            return None
        return {"id": 1, "block_key": args[0], "version": args[1],
                "active": True, "token_counts": args[-1]}


@pytest.mark.asyncio
async def test_version_none_auto_increments():
    conn = _FakeConn()
    with patch("app.services.prompt_token_counting.compute_token_counts", return_value={"gemini": 10}):
        result = await publish_block_version(
            conn, block_key="k", template_body="x", owner="o", created_by="c", version=None,
        )
    assert result["skipped"] is False
    assert result["version"] == 5  # from the MAX(version)+1 stub


@pytest.mark.asyncio
async def test_explicit_version_already_present_skips_without_computing():
    conn = _FakeConn(existing=True)
    fake_compute = MagicMock(return_value={"gemini": 999})
    with patch("app.services.prompt_token_counting.compute_token_counts", fake_compute):
        result = await publish_block_version(
            conn, block_key="k", template_body="x", owner="o", created_by="c", version=3,
        )
    assert result == {"skipped": True, "block_key": "k", "version": 3}
    fake_compute.assert_not_called()


@pytest.mark.asyncio
async def test_conflict_race_reports_skipped_not_a_fabricated_row():
    # Pre-check passed (didn't exist yet) but a concurrent publisher won
    # the actual INSERT -- ON CONFLICT DO NOTHING means no row comes back.
    # Must report skipped, never invent a row that wasn't actually written.
    conn = _FakeConn(existing=False, insert_result="race")
    with patch("app.services.prompt_token_counting.compute_token_counts", return_value={"gemini": 10}):
        result = await publish_block_version(
            conn, block_key="k", template_body="x", owner="o", created_by="c", version=3,
        )
    assert result == {"skipped": True, "block_key": "k", "version": 3}


@pytest.mark.asyncio
async def test_auto_validate_false_passes_through_verbatim():
    # react_block_seed.py's requirement: a seeded block's validated_at
    # describes a real historical review baked into its BlockSpec, not
    # "whoever ran the seed script, just now" -- auto_validate=False must
    # use exactly what's passed, including None, never compute now().
    conn = _FakeConn()
    captured = {}

    async def fetchrow_capture(sql, *args):
        if "INSERT INTO prompt_blocks" in sql:
            captured["validated_at"] = args[9]
            captured["validated_by"] = args[10]
            return {"id": 1, "block_key": args[0], "version": args[1],
                    "active": True, "token_counts": args[-1]}
        return None

    conn.fetchrow = fetchrow_capture
    with patch("app.services.prompt_token_counting.compute_token_counts", return_value={"gemini": 10}):
        await publish_block_version(
            conn, block_key="k", template_body="x", owner="o", created_by="c",
            version=1, activate=True, is_authority=False,
            auto_validate=False, validated_at=None, validated_by=None,
        )
    assert captured["validated_at"] is None
    assert captured["validated_by"] is None


@pytest.mark.asyncio
async def test_derived_block_kind_never_computes_tokens():
    conn = _FakeConn()
    fake_compute = MagicMock(return_value={"gemini": 999})
    with patch("app.services.prompt_token_counting.compute_token_counts", fake_compute):
        await publish_block_version(
            conn, block_key="k", template_body="{{ x }}", owner="o", created_by="c",
            block_kind="derived", version=1,
        )
    fake_compute.assert_not_called()
