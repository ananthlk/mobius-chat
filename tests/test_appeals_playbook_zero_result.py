"""appeals_get_playbook must signal "no useful content" when found but
empty (2026-08-07, Ananth, directly, live-query finding).

Live trace: a COB reconsideration question called appeals_get_playbook
7 times in a row across rounds 2-9 of a 10-round budget, each time
getting "found": True but neither deadline_appeal_days nor
submission_method populated ("?d deadline · " emit line) -- and nothing
stopped it, because the handler always returned signal=None regardless
of content, so ReactRetryGuard._is_zero_result (which only fires on
signal=="no_sources") never classified these as failures.
consecutive_failures_per_tool never incremented; the tool-exhaustion
block that exists specifically to prevent this never fired.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import _execute_tool
from app.pipeline.react_retry_guard import ReactRetryGuard


def _make_ctx() -> PipelineContext:
    ctx = PipelineContext(correlation_id="pb-test", thread_id=None, message="COB reconsideration")
    ctx.effective_message = ctx.message
    return ctx


def _mock_get_response(json_body: dict):
    resp = MagicMock()
    resp.json.return_value = json_body
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


class TestAppealsGetPlaybookZeroResult:
    def test_found_with_real_data_is_usable(self):
        ctx = _make_ctx()
        with patch("app.pipeline.react_loop.httpx.Client", return_value=_mock_get_response(
            {"deadline_appeal_days": 90, "submission_method": "provider portal"}
        )):
            r = _execute_tool("appeals_get_playbook", {"payor": "Sunshine Health", "carc": 22}, ctx, None)
        assert r["success"] is True
        assert r["signal"] is None
        assert r["section_hint"] is not None

    def test_found_but_content_empty_signals_no_sources(self):
        """The exact live regression: found=True, but neither field
        populated -- must now be classified as a zero-result, not a
        silent success."""
        ctx = _make_ctx()
        with patch("app.pipeline.react_loop.httpx.Client", return_value=_mock_get_response({})):
            r = _execute_tool("appeals_get_playbook", {"payor": "Sunshine Health", "carc": 22}, ctx, None)
        assert r["success"] is True  # HTTP call itself succeeded
        assert r["signal"] == "no_sources"
        assert r["section_hint"] is None

    def test_found_with_only_deadline_is_still_usable(self):
        ctx = _make_ctx()
        with patch("app.pipeline.react_loop.httpx.Client", return_value=_mock_get_response(
            {"deadline_appeal_days": 60}
        )):
            r = _execute_tool("appeals_get_playbook", {"payor": "Sunshine Health", "carc": 22}, ctx, None)
        assert r["signal"] is None

    def test_found_with_only_method_is_still_usable(self):
        ctx = _make_ctx()
        with patch("app.pipeline.react_loop.httpx.Client", return_value=_mock_get_response(
            {"submission_method": "certified mail"}
        )):
            r = _execute_tool("appeals_get_playbook", {"payor": "Sunshine Health", "carc": 22}, ctx, None)
        assert r["signal"] is None


class TestRetryGuardNowCatchesTheLoop:
    """Confirms the fix actually closes the loop: the retry-guard
    machinery already existed and already had an exhaustion threshold --
    it just never saw a failure signal from this tool before."""

    def test_repeated_empty_playbook_calls_trip_exhaustion(self):
        ctx = _make_ctx()
        guard = ReactRetryGuard()
        with patch("app.pipeline.react_loop.httpx.Client", return_value=_mock_get_response({})):
            for rn in range(1, 4):
                r = _execute_tool(
                    "appeals_get_playbook",
                    {"payor": "Sunshine Health", "carc": 22, "note": f"attempt {rn}"},
                    ctx, None,
                )
                guard.record_result(
                    tool="appeals_get_playbook", inputs={"payor": "Sunshine Health", "carc": 22, "note": f"attempt {rn}"},
                    result=r, round=rn, results_count_before=rn - 1,
                )
        assert guard.consecutive_failures_per_tool.get("appeals_get_playbook", 0) >= 2
        blocked = guard.should_block(
            tool="appeals_get_playbook",
            inputs={"payor": "Sunshine Health", "carc": 22, "note": "attempt 4"},
            current_results_count=3,
        )
        assert blocked is not None
        assert blocked.error_code == "tool_exhausted"
        assert "appeals_get_playbook" in guard.failure_hint_for_prompt()
