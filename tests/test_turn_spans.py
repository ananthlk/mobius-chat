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
        # "rag" is not a schema node key, so the child inherits the parent's
        # node and keeps "rag" as its local label — node -> span TREE.
        assert child.module == "react_loop" and child.label == "rag"
        assert child.depth == 1
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
                "columns": ["span_id", "parent_span_id", "module", "label", "depth",
                            "wall_ms", "llm_ms", "counts", "model_mix",
                            "rich_evidence", "chat_mode"],
                "rows": [[r["span_id"], r["parent_span_id"], r["module"], r["label"],
                          r["depth"], r["wall_ms"], r["llm_ms"], r["counts"],
                          r["model_mix"], r["rich_evidence"], r["chat_mode"]] for r in rows],
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
        # both spans resolve to the react_loop NODE; the finer one is labelled
        assert {s["module"] for s in spans} == {"react_loop"}
        assert {s.get("label") for s in spans} == {None, "rag"}

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


# ── schema-node binding ─────────────────────────────────────────────

class TestNodeKeyBinding:
    """span.module is a schema node key, so spans and the schema can check
    each other. Ad-hoc names give two decompositions of one system that
    neither can falsify."""

    def test_known_node_key_is_kept(self):
        t = TurnTrace("cid-n1")
        with t.span("react_loop"):
            pass
        assert t.spans[0].module == "react_loop"
        assert t.spans[0].label is None

    def test_child_finer_than_a_node_inherits_the_parent_key(self):
        """node -> span TREE, not node -> span. Every span still resolves to
        a node, so a live node that produces no span is detectable."""
        t = TurnTrace("cid-n2")
        with t.span("react_loop"):
            with t.span("rag_call"):
                pass
        root, child = t.spans
        assert child.module == "react_loop", "inherits the node key"
        assert child.label == "rag_call", "keeps its own local name"

    def test_unknown_ROOT_span_is_reported_as_a_schema_gap(self, caplog):
        """A root span that is not a node means the schema is missing
        something the system does. It must be loud — a silently normalised
        name is how the map stops matching the territory."""
        t = TurnTrace("cid-n3")
        with caplog.at_level("WARNING"):
            with t.span("something_not_modelled"):
                pass
        assert any("not a schema node key" in r.getMessage() for r in caplog.records)
        assert t.spans[0].module == "something_not_modelled", "recorded as given, not silently renamed"


# ── the tool-selection three-layer split ────────────────────────────

class TestToolSelectionLayers:
    """Ananth 2026-09-09: the model said its feedback tool was broken; the
    backend answered 200 in 1.3s; the tool fired fully 73s later. Three
    layers could drop the call and today they produce IDENTICAL evidence:
    none. These counts separate them at n=1."""

    from app.telemetry.spans import (  # noqa: E402
        KIND_TOOL_DISPATCHED, KIND_TOOL_EMITTED, KIND_TOOL_OFFERED,
    )

    def _trace(self, *, offered, emitted, dispatched):
        t = TurnTrace("cid-tool")
        with t.span("tool_manifest", label="resolve_allowed_tools"):
            for x in offered:
                t.record(self.KIND_TOOL_OFFERED, x)
        with t.span("react_loop"):
            for x in emitted:
                t.record(self.KIND_TOOL_EMITTED, x)
            for x in dispatched:
                t.record(self.KIND_TOOL_DISPATCHED, x)
        return {(c["kind"], c["target"]): c["n"] for c in t.totals()["counts"]}

    def test_layer1_not_offered_is_distinguishable(self):
        """The manifest never contained the tool — allowed_tools filtered it."""
        c = self._trace(offered=["search_corpus"], emitted=[], dispatched=[])
        assert ("tool.offered", "product_feedback") not in c
        assert ("tool.emitted", "product_feedback") not in c

    def test_layer2_offered_but_planner_never_emitted_it(self):
        """Offered and available, model simply didn't pick it. Distinct from
        layer 1 — and the distinction is the diagnosis."""
        c = self._trace(offered=["product_feedback"], emitted=["__none__"], dispatched=[])
        assert c[("tool.offered", "product_feedback")] == 1
        assert ("tool.emitted", "product_feedback") not in c
        assert c[("tool.emitted", "__none__")] == 1, "emitting nothing is a positive observation"

    def test_layer3_dispatched_and_failed_vs_never_selected(self):
        """A dispatch that failed must not look like a tool that was never
        chosen. Outcome rides in the target for exactly this reason."""
        c = self._trace(offered=["product_feedback"], emitted=["product_feedback"],
                        dispatched=["product_feedback:failure"])
        assert c[("tool.dispatched", "product_feedback:failure")] == 1
        assert ("tool.dispatched", "product_feedback:success") not in c

    def test_the_three_layers_are_mutually_exclusive_on_one_turn(self):
        """The point of the split: given the counts, exactly one layer
        explains a missing tool call."""
        not_offered = self._trace(offered=["search_corpus"], emitted=["__none__"], dispatched=[])
        not_picked = self._trace(offered=["product_feedback"], emitted=["__none__"], dispatched=[])
        failed = self._trace(offered=["product_feedback"], emitted=["product_feedback"],
                             dispatched=["product_feedback:failure"])
        # Same user-visible symptom (no feedback captured), three different causes.
        assert ("tool.offered", "product_feedback") not in not_offered
        assert ("tool.offered", "product_feedback") in not_picked
        assert ("tool.dispatched", "product_feedback:failure") in failed


class TestDbIdentifier:
    """The mocked read-back cannot catch a wrong database key.

    Every span test patches db_execute/db_query, so `_DB` is never exercised
    against the real client contract. Shipped with _DB = "mobius_chat" (the
    PHYSICAL database name) instead of "chat" (db_client's LOGICAL key), all
    15 tests passed and every live write failed with

        connection_error: No fallback URL for database 'mobius_chat'

    Only the live turn caught it — which is the argument for the acceptance
    criterion being a REAL turn rather than a mocked round trip. This pins the
    value so the same mistake can't return silently.
    """

    def test_db_key_matches_the_other_storage_modules(self):
        import app.storage.threads as threads
        import app.storage.turn_spans as spans
        import app.storage.turns as turns
        assert spans._DB == threads._DB == turns._DB, (
            f"turn_spans uses _DB={spans._DB!r} but the rest of app/storage uses "
            f"{threads._DB!r} — db_client resolves the LOGICAL key, not the "
            f"physical database name"
        )

    def test_db_key_resolves_in_db_client(self):
        """The key must be one db_client can actually map to a URL."""
        import inspect
        import app.db_client as db_client
        import app.storage.turn_spans as spans
        src = inspect.getsource(db_client._get_fallback_url)
        assert f'"{spans._DB}"' in src, (
            f"_DB={spans._DB!r} does not appear in _get_fallback_url's mapping; "
            f"writes will fail with connection_error at runtime while every "
            f"mocked test still passes"
        )
