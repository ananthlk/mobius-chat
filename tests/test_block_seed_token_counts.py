"""Governor seat, 2026-09-10: flagged block_seed.py (the integrator/
enricher seeder) as a THIRD independent writer to prompt_blocks after
admin_prompts.create_block_version() and react_block_seed.py's seed()
were both already fixed to route through publish_block_version(). "The
shape is narrowed, not closed." Confirmed live: 5 of this file's 7
block_keys (forced_json, hipaa_context, module.enricher.answer/.blended/
.factual) were still at the v1 this seed wrote, actively serving live
compositions today. Fixed the same way.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.block_seed import build_plan, seed


class _FakeConn:
    """Every (block_key, version=1) already exists -- matches this file's
    real state (all 7 of its block_keys already have a v1 row)."""

    async def fetchval(self, sql, *args):
        if "SELECT 1 FROM prompt_blocks" in sql:
            return 1
        return 0

    async def fetchrow(self, sql, *args):
        return None  # composition ON CONFLICT DO NOTHING -> no row

    async def execute(self, sql, *args):
        pass


@pytest.mark.asyncio
async def test_already_present_blocks_never_compute_token_counts():
    conn = _FakeConn()
    with patch(
        "app.services.prompt_token_counting.compute_token_counts",
        MagicMock(return_value={"gemini": 999}),
    ) as fake_compute:
        await seed(conn)
    fake_compute.assert_not_called()


@pytest.mark.asyncio
async def test_seed_routes_through_publish_block_version():
    # Not a behavior test (covered above and by test_prompt_block_publish.py)
    # -- a structural lock so this file can't quietly grow its own
    # INSERT INTO prompt_blocks again the way it originally had one.
    resolved, _ = build_plan()
    assert len(resolved) > 0  # sanity: this file still defines real blocks
    with patch(
        "app.services.prompt_block_publish.publish_block_version",
        AsyncMock(return_value={"skipped": True}),
    ) as fake_publish:
        await seed(_FakeConn())
    assert fake_publish.call_count == len(resolved)
