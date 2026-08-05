"""Task #32 — SSE stream keeps polling for late progress events (e.g.
bandit_reward_persisted from post_run_adjudication, which runs as a
fire-and-forget thread started AFTER the main response is published)
instead of closing the instant the terminal 'completed' event is found.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_stream_relays_late_events_after_completed():
    from app.api.chat import chat_stream

    cid = "test-cid-grace-window"
    completed_payload = {"status": "completed", "message": "done"}

    late_event = {"event": "bandit_reward_persisted", "data": {"stage": "integrator", "quality_score": 0.9}}
    # First poll (pre-completion): no progress events, no response yet on first call,
    # response available from the second call onward.
    call_count = {"n": 0}

    def _get_response(_cid):
        call_count["n"] += 1
        return completed_payload if call_count["n"] >= 1 else None

    # get_progress_events_from_db: nothing pre-completion, then the late
    # event appears once during the grace window, then nothing more.
    db_call_count = {"n": 0}

    def _get_progress_events_from_db(_cid, after_id=0):
        db_call_count["n"] += 1
        if db_call_count["n"] == 3:
            return [(1, late_event)]
        return []

    mock_queue = MagicMock()
    mock_queue.get_response.return_value = None  # forces fallback to app.storage.get_response

    with patch.dict(os.environ, {"CHAT_STREAM_POST_COMPLETE_GRACE_S": "1", "CHAT_QUEUE_TYPE": "redis"}):
        with patch("app.api.chat.get_queue", return_value=mock_queue), \
             patch("app.api.chat.get_response", side_effect=_get_response), \
             patch("app.api.chat.get_progress_events_from_db", side_effect=_get_progress_events_from_db), \
             patch("app.api.chat.get_config") as mock_cfg:
            mock_cfg.return_value.queue_type = "redis"

            resp = await chat_stream(cid)
            chunks = []
            async for chunk in resp.body_iterator:
                chunks.append(chunk)

    joined = "".join(c if isinstance(c, str) else c.decode() for c in chunks)
    assert '"event": "completed"' in joined or '"event":"completed"' in joined
    # The late bandit_reward_persisted event, which only appears on the
    # THIRD progress-event poll (i.e. during the grace window, after
    # "completed" was already yielded), must still show up in the stream.
    assert "bandit_reward_persisted" in joined
