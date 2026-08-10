"""Tests for app.worker.run's terminal-failure publishing (Task #78).

Root cause: a turn can go silent forever if an exception escapes
run_pipeline() outside the specific branches that already call
_publish_failed. Neither deadline-enforcement path (main-thread signal.alarm,
or the background daemon-thread timeout) had a catch-all for a *generic*
exception -- it propagated to the queue consumer's own blanket except,
which only logs, never publishes. The client then polls GET /chat/response
forever with no terminal status. Root-caused live: cid ca219529, went
silent right after react round-1 preflight, no crash log, no
turn_deadline_exceeded log -- the pipeline thread died via an uncaught
exception well inside the 300s deadline, so the deadline path never
engaged at all.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from app.worker.run import process_one


def _base_payload(**overrides) -> dict:
    base = {"message": "hello", "thread_id": None}
    base.update(overrides)
    return base


class TestPath1MainThreadUnhandledException:
    """pytest runs on the main thread, so calling process_one() directly
    naturally exercises Path 1 (signal.alarm)."""

    def test_unhandled_exception_publishes_failed_response(self):
        mock_queue = MagicMock()
        with (
            patch("app.pipeline.orchestrator.run_pipeline", side_effect=RuntimeError("boom")),
            patch("app.queue.get_queue", return_value=mock_queue),
            patch("app.storage.progress.get_checkpoint", return_value=None),
            patch("app.storage.progress.try_finalize", return_value=True),
        ):
            process_one("cid-path1-exc", _base_payload())

        mock_queue.publish_response.assert_called_once()
        cid_arg, payload = mock_queue.publish_response.call_args[0]
        assert cid_arg == "cid-path1-exc"
        assert payload["status"] == "failed"
        assert payload["error"] == "unhandled_pipeline_exception"
        assert "message" in payload

    def test_already_finalized_turn_skips_duplicate_publish(self):
        """If _publish_failed (or a completed publish) already claimed
        finalization from inside run_pipeline before the exception
        propagated, the worker-level publish must not double-publish."""
        mock_queue = MagicMock()
        with (
            patch("app.pipeline.orchestrator.run_pipeline", side_effect=RuntimeError("boom")),
            patch("app.queue.get_queue", return_value=mock_queue),
            patch("app.storage.progress.get_checkpoint", return_value=None),
            patch("app.storage.progress.try_finalize", return_value=False),
        ):
            process_one("cid-path1-already-finalized", _base_payload())

        mock_queue.publish_response.assert_not_called()

    def test_deadline_exceeded_still_publishes_deadline_specific_failure(self):
        """Regression guard: generalizing _publish_deadline_failure into
        _publish_terminal_failure must not change the deadline case's own
        error code/message."""
        from app.worker.run import _TurnDeadlineExceeded

        mock_queue = MagicMock()
        with (
            patch("app.pipeline.orchestrator.run_pipeline", side_effect=_TurnDeadlineExceeded("turn exceeded deadline")),
            patch("app.queue.get_queue", return_value=mock_queue),
            patch("app.storage.progress.get_checkpoint", return_value=None),
            patch("app.storage.progress.try_finalize", return_value=True),
        ):
            process_one("cid-path1-deadline", _base_payload())

        mock_queue.publish_response.assert_called_once()
        _, payload = mock_queue.publish_response.call_args[0]
        assert payload["error"] == "turn_deadline_exceeded"
        assert payload["deadline_s"] > 0


class TestPath2DaemonThreadUnhandledException:
    """Running process_one() from a background thread exercises Path 2
    (daemon-thread + Event timeout) -- the path that silently swallowed
    exceptions before this fix."""

    def test_unhandled_exception_publishes_failed_response(self):
        mock_queue = MagicMock()
        with (
            patch("app.pipeline.orchestrator.run_pipeline", side_effect=RuntimeError("boom")),
            patch("app.queue.get_queue", return_value=mock_queue),
            patch("app.storage.progress.get_checkpoint", return_value=None),
            patch("app.storage.progress.try_finalize", return_value=True),
        ):
            t = threading.Thread(target=process_one, args=("cid-path2-exc", _base_payload()))
            t.start()
            t.join(timeout=10)
            assert not t.is_alive()

        mock_queue.publish_response.assert_called_once()
        cid_arg, payload = mock_queue.publish_response.call_args[0]
        assert cid_arg == "cid-path2-exc"
        assert payload["status"] == "failed"
        assert payload["error"] == "unhandled_pipeline_exception"
