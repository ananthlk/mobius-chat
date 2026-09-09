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


class TestSourceClassification:
    """Fleet percentiles are only meaningful over ONE population.

    Deploy smoke turns hit cold connection pools at ~400-500ms per DB call;
    real turns run at ~30-39ms. Mixed, the p50 described neither — correct
    arithmetic over the wrong sampling frame.
    """

    def test_uuid_is_real_and_scripted_ids_are_smoke(self):
        import uuid as _u
        from app.telemetry.spans import classify_source
        assert classify_source(str(_u.uuid4())) == "real"
        for cid in ("sel-cid-3", "smoke-1", "cid-local-probe", "abc", ""):
            assert classify_source(cid) == "smoke", cid

    def test_unrecognised_shape_errs_toward_smoke(self):
        """Polluting the real baseline with synthetic turns is the failure
        that matters; a synthetic turn wrongly excluded is merely absent."""
        from app.telemetry.spans import classify_source
        assert classify_source("not-a-uuid-at-all") == "smoke"

    def test_declared_source_wins_over_inference(self):
        """An eval harness knows what it is; a UUID cid must not override it."""
        import uuid as _u
        from app.telemetry.spans import classify_source
        assert classify_source(str(_u.uuid4()), "eval") == "eval"


# ── concurrent siblings (db/schema/064) ──────────────────────────────────
# The integrator fans three LLM calls out on a thread pool. Two things go wrong
# without the `concurrent` flag, and BOTH are silent:
#   1. their walls sum to more than the parent's, so the matrix stops being
#      additive — a total that isn't the sum of its parts;
#   2. the parent's processing clamps to 0, hiding real code time.
# These run entirely in-process on purpose: the bug that shipped the last
# version of this module was masked by tests that patched db_execute, so a
# guard that needs a database is a guard that does not run.

def test_concurrent_children_use_max_not_sum():
    from app.storage.turn_spans import turn_matrix
    parent = {"span_id": "p", "parent_span_id": None, "module": "integrate",
              "label": None, "depth": 0, "wall_ms": 5000.0, "llm_ms": 0.0,
              "counts": [], "concurrent": False}
    kids = [
        {"span_id": f"k{i}", "parent_span_id": "p", "module": "integrate",
         "label": f"llm:call_{i}", "depth": 1, "wall_ms": ms, "llm_ms": ms,
         "counts": [{"kind": "llm", "target": f"call_{i}", "n": 1, "ms": ms}],
         "concurrent": True}
        for i, ms in enumerate((4000.0, 3000.0, 2500.0))
    ]
    m = turn_matrix("cid-x", spans=[parent] + kids)
    by = {r["label"] or r["module"]: r for r in m["rows"]}
    # 5000 wall, the block cost max(4000)=4000 -> 1000ms is genuinely OURS.
    # Summing the children (9500) would have clamped this to 0.
    assert by["integrate"]["processing_ms"] == 1000.0
    # Children never report an external call's wait as our processing.
    assert all(by[f"llm:call_{i}"]["processing_ms"] == 0.0 for i in range(3))


def test_add_completed_emits_both_llm_ms_and_a_count():
    """The matrix reads its llm column from COUNTS, not Span.llm_ms. A span
    that sets one without the other shows model time as processing."""
    from app.telemetry.spans import TurnTrace, KIND_LLM
    tr = TurnTrace("cid-y")
    with tr.span("integrate"):
        tr.add_completed("integrate", "llm:a", wall_ms=900.0, llm_ms=900.0,
                         kind=KIND_LLM, target="a", rollup_llm_ms=0.0,
                         concurrent=True)
    sp = [s for s in tr.spans if s.label == "llm:a"][0]
    assert sp.llm_ms == 900.0
    assert [c for c in sp.counts.values() if c.kind == KIND_LLM][0].ms == 900.0
    assert sp.concurrent is True


def test_close_all_closes_the_turn_root_and_any_early_return_leftovers():
    """An unclosed span has wall_ms 0 — it renders as a node that took no time,
    which is the same lie as a node that was never instrumented."""
    from app.telemetry.spans import TurnTrace
    tr = TurnTrace("cid-z")
    tr.open_span("run_pipeline", label="turn:copilot")
    tr.open_span("state_load")          # simulate an early return leaving it open
    tr.close_all()
    assert not tr._open_cms
    assert all(s.wall_ms >= 0.0 for s in tr.spans)
    # The root must be the ONLY depth-0 span, so totals cover the whole turn
    # including the gaps between stages.
    assert [s.module for s in tr.spans if s.depth == 0] == ["run_pipeline"]


def test_dropped_measurement_is_counted_not_swallowed():
    """record_ambient with no active trace used to return silently. That is how
    11.5s of integrator model time was read as our own processing.

    Asserts DELTAS, never the whole dict: _ORPHANED is process-global and the
    progress-writer daemon thread adds to it from outside any test. An exact
    equality here passes alone and fails in a full run — which is the
    order-dependence this suite already has four of.
    """
    from app.telemetry import spans as sp
    # Guarantee there is no ambient trace. Another test may have left one bound
    # (the suite runs in random order), and record_ambient would then attribute
    # the call instead of orphaning it — so this asserted nothing about the
    # behaviour under test and failed only on some seeds.
    _tok = sp.set_active(None)
    try:
        before = sp.orphaned_ms().get("llm", 0.0)
        sp.record_ambient(sp.KIND_LLM, "gemini-2.5-flash", ms=4200.0)
        assert sp.orphaned_ms().get("llm", 0.0) - before >= 4200.0
        # A zero-duration record is not a lost measurement; not counted.
        mid = sp.orphaned_ms().get("llm", 0.0)
        sp.record_ambient(sp.KIND_LLM, "x", ms=0.0)
        # A zero-duration record is not a lost measurement and must add
        # nothing. No daemon in this suite orphans LLM time, so this one can
        # stay exact — unlike db.write above.
        assert sp.orphaned_ms().get("llm", 0.0) == mid
    finally:
        sp.reset_active(_tok)


def test_orphaned_db_writes_are_counted_but_not_warned(caplog):
    """Daemon threads (the progress writer) run outside any turn BY DESIGN and
    orphan db writes constantly. Warning on those is noise that teaches a reader
    to ignore the line — which would take the real llm signal with it."""
    import logging
    from app.telemetry import spans as sp
    _tok = sp.set_active(None)   # see the note in the test above
    try:
        b_db = sp.orphaned_ms().get("db.write", 0.0)
        b_llm = sp.orphaned_ms().get("llm", 0.0)
        with caplog.at_level(logging.WARNING, logger="app.telemetry.spans"):
            sp.record_ambient(sp.KIND_DB_WRITE, "chat_progress_events", ms=70453.0)
            assert "AT RECORD TIME" not in caplog.text
            sp.record_ambient(sp.KIND_LLM, "gemini-2.5-flash", ms=4200.0)
            assert "AT RECORD TIME" in caplog.text
        # Both COUNTED — the gauge must not lie just because one is quiet.
        #
        # `>=`, not `==`: the progress-writer daemon orphans db writes from
        # ANOTHER THREAD while this test runs, so an exact delta is a race that
        # passes locally and fails in a full suite run. It can only ever ADD, so
        # a lower bound is the strongest claim that is actually true. (This test
        # has now been the order-dependent one twice — first for assuming no
        # ambient trace, then for assuming exclusive access to the counter.)
        assert sp.orphaned_ms().get("db.write", 0.0) - b_db >= 70453.0
        assert sp.orphaned_ms().get("llm", 0.0) - b_llm >= 4200.0
    finally:
        sp.reset_active(_tok)


def test_acquire_time_is_reported_but_not_added_to_the_db_column():
    """Connection-acquire time sits INSIDE the db.read total (db_query times the
    whole call), so it is reported alongside db_read_ms, never summed into it.

    This is the instrument for a real finding: with CHAT_DB_MODE=direct every
    query acquires its own connection, and a pooled acquire costs a SELECT 1
    plus a commit before the caller's SQL runs. A count keyed on the target
    table cannot see that — four reads each reporting n=1 while the wall says
    1.2s is exactly what acquire-time wearing query-time's clothes looks like.
    """
    from app.storage.turn_spans import turn_matrix
    sp = {"span_id": "s", "parent_span_id": None, "module": "state_load",
          "label": None, "depth": 0, "wall_ms": 600.0, "llm_ms": 0.0,
          "concurrent": False,
          "counts": [
              {"kind": "db.read", "target": "chat_state", "n": 4, "ms": 500.0},
              {"kind": "db.acquire", "target": "pool", "n": 4, "ms": 420.0},
          ]}
    m = turn_matrix("cid-a", spans=[sp])
    row = m["rows"][0]
    assert row["db_read_ms"] == 500.0          # unchanged, not 920
    assert row["db_acquire_ms"] == 420.0       # reported separately
    assert row["db_acquires"] == 4
    # 600 wall - 500 db = 100 of our own code. Acquire must not eat into it.
    assert row["processing_ms"] == 100.0
    assert m["totals"]["db_acquire_ms"] == 420.0
