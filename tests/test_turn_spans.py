"""P2b acceptance: a span written on a turn must be readable and renderable.

THE ACCEPTANCE CRITERION IS A SAME-TURN READ-BACK, and it asserts against the
DESTINATION OF RECORD — what read_spans() returns from the table — not an
emitter log, not the in-memory TurnTrace, not the nearest artifact that
happens to carry the right-looking field.

That distinction is not pedantry. On 2026-09-09 a correlation_id fix was
reported "verified end-to-end" on the strength of a log line containing the
field; the log line was a different record (the PHI diagnostics envelope)
while the destination of record still wrote NULL. A read-back of the wrong
artifact is indistinguishable from a successful one.
"""
from __future__ import annotations

from unittest.mock import patch

from app.storage.turn_spans import read_spans, save_spans, summarize
from app.telemetry.spans import KIND_DB_WRITE, KIND_LLM, TurnTrace


# ── the recorder ────────────────────────────────────────────────────

class TestTurnTrace:
    def test_nesting_sets_parent_and_depth(self):
        t = TurnTrace("cid-1")
        with t.span("react_loop"):
            with t.span("rag"):
                pass
        root, child = t.spans
        assert root.module == "react_loop" and root.depth == 0
        assert root.parent_span_id is None
        assert child.module == "rag" and child.depth == 1
        assert child.parent_span_id == root.span_id

    def test_a_loop_is_distinguishable_from_one_slow_call(self):
        """The whole point of the schema: n separates these two shapes.

        Forty 2ms writes and one 80ms write have identical total duration.
        A flat per-module timing cannot tell them apart — not lossily, at
        all — and the forty is the bug being hunted.
        """
        loop = TurnTrace("cid-loop")
        with loop.span("react_loop"):
            for _ in range(40):
                loop.record(KIND_DB_WRITE, "chat_state", ms=2.0)

        slow = TurnTrace("cid-slow")
        with slow.span("react_loop"):
            slow.record(KIND_DB_WRITE, "chat_state", ms=80.0)

        lt, st = loop.totals(), slow.totals()
        assert lt["counts"][0]["ms"] == st["counts"][0]["ms"] == 80.0  # identical duration
        assert lt["counts"][0]["n"] == 40                              # ...but distinguishable
        assert st["counts"][0]["n"] == 1

    def test_counts_carry_the_target_not_just_a_number(self):
        """40 writes to one table is a loop; 40 across 40 tables is a busy turn."""
        t = TurnTrace("cid-2")
        with t.span("m"):
            for i in range(40):
                t.record(KIND_DB_WRITE, f"table_{i}")
        counts = t.totals()["counts"]
        assert len(counts) == 40
        assert all(c["n"] == 1 for c in counts)

    def test_llm_time_rolls_up_and_self_ms_excludes_it(self):
        t = TurnTrace("cid-3")
        with t.span("outer") as outer:
            with t.span("inner"):
                t.record(KIND_LLM, "gemini-2.5-pro", ms=20000.0)
        inner = t.spans[1]
        assert inner.llm_ms == 20000.0
        assert outer.llm_ms == 20000.0, "parent must include child llm time"
        assert outer.self_ms == max(0.0, outer.wall_ms - 20000.0)

    def test_span_that_raises_still_records_a_duration(self):
        t = TurnTrace("cid-4")
        try:
            with t.span("boom"):
                raise RuntimeError("x")
        except RuntimeError:
            pass
        assert len(t.spans) == 1 and t.spans[0].wall_ms >= 0.0

    def test_count_outside_a_span_is_logged_not_silently_dropped(self, caplog):
        """A count with nowhere to go means a missing span. Dropping it quietly
        would make the instrument under-report exactly when instrumentation is
        incomplete — the failure it exists to detect."""
        t = TurnTrace("cid-5")
        with caplog.at_level("WARNING"):
            t.record(KIND_DB_WRITE, "chat_state")
        assert any("outside any span" in r.getMessage() for r in caplog.records)


# ── the read-back ───────────────────────────────────────────────────

class TestSameTurnReadBack:
    """Write → read from the TABLE → summarize. All three on one turn."""

    def _fake_db(self, store):
        def _exec(sql, db, params=None):
            store.append(dict(params or {}))
            return {"ok": True}

        def _query(sql, db, params=None):
            rows = [r for r in store if r["cid"] == (params or {}).get("cid")]
            import json as _j
            return {
                "columns": ["span_id", "parent_span_id", "module", "depth",
                            "wall_ms", "llm_ms", "counts", "model_mix",
                            "rich_evidence", "chat_mode"],
                "rows": [[r["span_id"], r["parent_span_id"], r["module"], r["depth"],
                          r["wall_ms"], r["llm_ms"], r["counts"], r["model_mix"],
                          r["rich_evidence"], r["chat_mode"]] for r in rows],
            }
        return _exec, _query

    def test_write_then_read_then_render_on_one_turn(self):
        store: list = []
        _exec, _query = self._fake_db(store)

        t = TurnTrace("cid-acceptance")
        with t.span("react_loop"):
            with t.span("rag"):
                for _ in range(40):
                    t.record(KIND_DB_WRITE, "chat_state", ms=2.0)
            t.record(KIND_LLM, "gemini-2.5-pro", ms=20000.0)

        with patch("app.storage.turn_spans.db_execute", _exec):
            written = save_spans("cid-acceptance", t.to_rows(),
                                 chat_mode="copilot",
                                 model_mix=[{"provider": "vertex", "model": "gemini-2.5-pro", "n": 1}],
                                 rich_evidence=True)
        assert written == 2, "both spans stored"

        # THE READ-BACK: from the table, not from `t`.
        with patch("app.storage.turn_spans.db_query", _query):
            spans = read_spans("cid-acceptance")
        assert len(spans) == 2
        assert {s["module"] for s in spans} == {"react_loop", "rag"}

        # THE RENDER INPUT: what the diagnostics panel is handed.
        summary = summarize(spans)
        loop = next(c for c in summary["counts"] if c["target"] == "chat_state")
        assert loop["n"] == 40, "the loop survives the round trip to storage"
        assert summary["counts"][0]["n"] >= summary["counts"][-1]["n"], "sorted by n desc"
        assert summary["model_mix"][0]["model"] == "gemini-2.5-pro"
        assert summary["rich_evidence"] is True
        assert summary["chat_mode"] == "copilot"

    def test_write_failure_is_reported_not_swallowed(self, caplog):
        def _exec(sql, db, params=None):
            return {"error": "connection refused"}
        t = TurnTrace("cid-fail")
        with t.span("m"):
            pass
        with patch("app.storage.turn_spans.db_execute", _exec):
            with caplog.at_level("WARNING"):
                written = save_spans("cid-fail", t.to_rows())
        assert written == 0
        assert any("write failed" in r.getMessage() for r in caplog.records), \
            "a missing span row must be traceable to a logged cause, not look like a fast turn"
