"""Governor seat thread, 2026-09-10: react_block_seed.py's seed() predated
migration 066 and never populated prompt_blocks.token_counts, unlike
admin_prompts.create_block_version's publish path -- a second, inconsistent
way to write a block where only one computed a count. Found while adding
react.critical_rules v8's citation through this seed file. Fixed to compute
token_counts too, gated the same way (static/conditional only, skip the
network call entirely for a (block_key, version) already in the DB so a
safe-to-rerun seed() doesn't cost N real API calls on every rerun for
blocks that haven't changed).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.react_block_seed import _react_block_specs, seed


class _FakeConn:
    """fetchval always answers `already_present`; execute/fetchrow are
    no-ops that just record calls (seed()'s composition-upsert path also
    runs, but this test only cares about the block-upsert/token-count
    decision, not composition bookkeeping)."""

    def __init__(self, already_present: bool):
        self._already_present = already_present
        self.executed: list[tuple] = []

    async def fetchval(self, sql, *args):
        if "SELECT 1 FROM prompt_blocks" in sql:
            return 1 if self._already_present else None
        return 0  # composition version fetchval paths, if any -- harmless default

    async def fetchrow(self, sql, *args):
        return None  # composition INSERT ... ON CONFLICT DO NOTHING -> no row

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


@pytest.mark.asyncio
async def test_already_present_blocks_never_compute_token_counts():
    conn = _FakeConn(already_present=True)
    with patch(
        "app.services.prompt_token_counting.compute_token_counts",
        MagicMock(return_value={"gemini": 999}),
    ) as fake_compute:
        await seed(conn)
    fake_compute.assert_not_called()


@pytest.mark.asyncio
async def test_new_static_and_conditional_blocks_compute_token_counts():
    specs = _react_block_specs()
    countable = [s for s in specs if s.block_kind in ("static", "conditional")]
    conn = _FakeConn(already_present=False)
    with patch(
        "app.services.prompt_token_counting.compute_token_counts",
        MagicMock(return_value={"gemini": 123}),
    ) as fake_compute:
        await seed(conn)
    assert fake_compute.call_count == len(countable)
    assert fake_compute.call_count > 0  # sanity: react.critical_rules etc. really are in this set
