"""Phase 0.16 — two surgical fixes observed in production logs.

0.16a: web_scrape timeout actually fires at the cap (not cap + worker-drain).
  Pre-0.16, ``with ThreadPoolExecutor(...) as _pool`` blocked on __exit__
  waiting for the worker to drain — a scrape that exceeded 30s by 8s held
  the tool handler for the full 38s instead of returning at 30s. The
  fix manages the pool explicitly and calls ``shutdown(wait=False)`` on
  timeout so the handler returns immediately.

0.16b: the LLM-based JSON repair path (third retry tier) is deleted.
  ``_parse_answer_card`` already runs stdlib json + the json_repair library
  — adding a third LLM call was pure overhead. The cost was real: one of
  those LLM repair calls hit Groq's daily-TPD quota in production logs.
"""

from __future__ import annotations

import concurrent.futures as _cf
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── 0.16a: timeout fires at the cap ────────────────────────────────────────


class TestScrapeTimeoutPattern:
    """Regression for the TPE __exit__ drain bug.

    We can't easily exercise the actual `_execute_tool` code path from
    a unit test (it requires a PipelineContext + emitter). Instead we
    assert the *pattern* is correct by reading the source and checking
    the two invariants that matter:

    1. No ``with ThreadPoolExecutor(...)`` wrapping the timed-out call.
    2. A ``shutdown(wait=False, cancel_futures=True)`` is called on
       timeout.
    """

    def _scrape_block(self) -> str:
        p = Path(__file__).parent.parent / "app" / "pipeline" / "react_loop.py"
        text = p.read_text()
        # Extract the web_scrape branch region by looking for the canonical
        # marker introduced in Phase 0.16a.
        marker = "0.16a fix: construct the pool manually"
        assert marker in text, (
            "Phase 0.16a marker is missing — has the fix been reverted?"
        )
        idx = text.find(marker)
        # Look ~60 lines forward to capture the whole timed block.
        return text[idx:idx + 2500]

    def test_no_with_thread_pool_for_scrape(self):
        """The broken pattern was ``with ThreadPoolExecutor(...) as _pool``.
        Using a ``with`` block blocks shutdown on worker-drain — the exact
        bug we fixed.
        """
        block = self._scrape_block()
        # There's no context-manager usage of ThreadPoolExecutor in the
        # web_scrape block now; a raw constructor + explicit shutdown instead.
        assert "with _cf.ThreadPoolExecutor" not in block, (
            "ThreadPoolExecutor is still used as a context manager — this "
            "re-introduces the drain-on-exit bug fixed in Phase 0.16a"
        )

    def test_timeout_path_calls_shutdown_wait_false(self):
        block = self._scrape_block()
        assert "shutdown(wait=False" in block, (
            "timeout branch must call pool.shutdown(wait=False, cancel_futures=True) "
            "to return immediately"
        )

    def test_success_path_still_drains(self):
        """On normal completion, shutdown(wait=True) is fine — no timeout pressure."""
        block = self._scrape_block()
        assert "shutdown(wait=True)" in block, (
            "normal-completion path should still call shutdown(wait=True) "
            "for clean cleanup"
        )


class TestThreadPoolTimeoutSemanticsMinimal:
    """Tiny end-to-end test of the pattern itself (not the scrape code) to
    lock in that shutdown(wait=False) actually returns at the timeout.

    This is a belt-and-suspenders test — the source-text tests above catch
    regression; this one confirms the underlying Python primitive behaves
    as we expect even on future interpreter upgrades.
    """

    def test_pattern_returns_at_timeout_not_worker_completion(self):
        def slow_worker():
            time.sleep(2.0)
            return "done"

        pool = _cf.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(slow_worker)

        t0 = time.perf_counter()
        try:
            future.result(timeout=0.2)
            assert False, "expected TimeoutError"
        except _cf.TimeoutError:
            pool.shutdown(wait=False, cancel_futures=True)
        elapsed = time.perf_counter() - t0

        # We asked for 0.2s timeout. With shutdown(wait=False), the pattern
        # must return promptly — well before the worker's 2.0s completion.
        # Allow some slack for scheduling but assert we're nowhere near 2s.
        assert elapsed < 0.8, (
            f"pattern took {elapsed:.2f}s — should have returned at ~0.2s "
            f"without waiting for the 2s worker. This would indicate the "
            f"0.16a pattern regressed."
        )


# ── 0.16b: LLM-based repair is removed ─────────────────────────────────────


class TestLLMRepairRemoved:
    """The ``_repair_json`` function and its caller are gone.

    Three assertions:
      - the function definition no longer exists
      - the caller no longer exists
      - ``_parse_answer_card`` still handles stdlib + json_repair library
        (which is what remains after removing the LLM tier)
    """

    def _final_py(self) -> str:
        p = Path(__file__).parent.parent / "app" / "responder" / "final.py"
        return p.read_text()

    def test_repair_json_function_removed(self):
        text = self._final_py()
        assert "def _repair_json(" not in text, (
            "LLM-based _repair_json was deleted in Phase 0.16b — if this "
            "test fails, the function has been reintroduced. Prefer the "
            "json_repair library (already in _parse_answer_card) over an "
            "extra LLM call."
        )

    def test_no_caller_of_repair_json(self):
        text = self._final_py()
        assert "_repair_json(" not in text, (
            "something still calls _repair_json — it was deleted in 0.16b"
        )

    def test_retry_after_json_repair_emit_removed(self):
        """The UI emit ``Validator: retrying after JSON repair…`` only fired
        when the LLM repair ran. It's gone too."""
        text = self._final_py()
        assert "retrying after JSON repair" not in text

    def test_parse_answer_card_still_uses_json_repair_library(self):
        """The library-based repair (tier 2) remains — that's the whole
        reason we could delete tier 3 safely.
        """
        text = self._final_py()
        # _parse_answer_card iterates [json.loads, _json_repair_loads]
        assert "_json_repair_loads" in text
        # And the helper itself still imports the library
        assert "import json_repair" in text


class TestIntegratorPromptStillReferencesRepair:
    """The ``integrator_repair_system`` prompt in chat_config is unused by
    code now. It's still present as a config-visible field (back-compat
    for anyone reading the config export), but nothing imports it.
    Intentional — we avoid a config-schema break until a later cleanup.
    """

    def test_final_py_does_not_import_integrator_repair_system(self):
        p = Path(__file__).parent.parent / "app" / "responder" / "final.py"
        text = p.read_text()
        assert "integrator_repair_system" not in text, (
            "0.16b: final.py must no longer reference the repair-system "
            "prompt. It's left in chat_config for config-export stability "
            "but intentionally unused."
        )
