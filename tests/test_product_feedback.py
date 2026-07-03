"""Tests for the product_feedback skill: cadence logic, storage fail-closed,
and the skill handler. DB and the classifier service are mocked."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.storage.product_feedback as store
from app.skills.registry import SkillCall

NOW = datetime(2026, 7, 2, tzinfo=timezone.utc)


# ── evaluate_cadence (pure) ─────────────────────────────────────────────────

class TestCadence:
    def _st(self, **kw):
        base = {"threads_since_prompt": 0, "turns_since_prompt": 0}
        base.update(kw)
        return base

    def _eval(self, st, **kw):
        args = dict(user_id="u1", thread_turns=1, last_turn_failed=False,
                    nudged_this_thread=False, just_rated=False, now=NOW)
        args.update(kw)
        return store.evaluate_cadence(st, **args)

    def test_open_periodic_fires_on_thread_threshold(self):
        r = self._eval(self._st(threads_since_prompt=5))
        assert r and r["kind"] == "generic"

    def test_open_periodic_fires_on_turn_threshold(self):
        r = self._eval(self._st(turns_since_prompt=25))
        assert r and r["kind"] == "generic"

    def test_targeted_miss_after_failure(self):
        r = self._eval(self._st(threads_since_prompt=5), last_turn_failed=True)
        assert r and r["kind"] == "targeted_miss"

    def test_nothing_when_under_threshold(self):
        assert self._eval(self._st(threads_since_prompt=1)) is None

    def test_opted_out_suppresses_all(self):
        assert self._eval(self._st(threads_since_prompt=99, opted_out=True)) is None

    def test_just_rated_suppresses(self):
        assert self._eval(self._st(threads_since_prompt=9), just_rated=True) is None

    def test_nudged_this_thread_suppresses(self):
        assert self._eval(self._st(threads_since_prompt=9), nudged_this_thread=True) is None

    def test_snooze_active_suppresses(self):
        future = (NOW + timedelta(days=2)).isoformat()
        assert self._eval(self._st(threads_since_prompt=9, snooze_until=future)) is None

    def test_snooze_expired_allows(self):
        past = (NOW - timedelta(days=2)).isoformat()
        r = self._eval(self._st(threads_since_prompt=9, snooze_until=past))
        assert r is not None

    def test_csat_needs_substantive_thread(self):
        # short thread → no csat even though thread activity present
        r = self._eval(self._st(), thread_turns=1)
        # first-time NPS also gated on thread_turns>=min, so short thread => None
        assert r is None

    def test_csat_fires_on_resolved_thread(self):
        r = self._eval(self._st(last_nps_at=NOW.isoformat()), thread_turns=4)
        # NPS suppressed (just did it) → CSAT is next in priority
        assert r and r["kind"] == "csat"

    def test_nps_priority_over_csat_when_both_eligible(self):
        r = self._eval(self._st(), thread_turns=4)  # no nps history, substantive thread
        assert r and r["kind"] == "nps"

    def test_nps_suppressed_right_after_miss(self):
        r = self._eval(self._st(last_nps_at=NOW.isoformat()), thread_turns=4,
                       last_turn_failed=True)
        # NPS gated by miss, CSAT gated by miss → falls through to open (under threshold) → None
        assert r is None

    def test_nps_not_due_within_interval(self):
        recent = (NOW - timedelta(days=5)).isoformat()
        r = self._eval(self._st(last_nps_at=recent), thread_turns=4)
        assert r and r["kind"] == "csat"  # NPS not due yet, CSAT eligible


# ── storage fail-closed ─────────────────────────────────────────────────────

class TestStorageFailClosed:
    @pytest.fixture
    def _conn_err(self, monkeypatch):
        monkeypatch.setattr(store, "db_execute",
                            lambda *a, **k: {"error": {"code": "connection_error",
                                                       "message": "pool down"}})

    def test_open_insert_dev_returns_none(self, monkeypatch, _conn_err):
        monkeypatch.setenv("CHAT_ENV", "dev")
        assert store.insert_open_feedback(trigger="inline", category="bug",
                                          verbatim="x") is None

    def test_open_insert_prod_raises(self, monkeypatch, _conn_err):
        monkeypatch.setenv("CHAT_ENV", "prod")
        with pytest.raises(store.ProductFeedbackError):
            store.insert_open_feedback(trigger="inline", category="bug", verbatim="x")

    def test_open_insert_success_returns_uuid(self, monkeypatch):
        monkeypatch.setattr(store, "db_execute", lambda *a, **k: {"rows_affected": 1})
        fid = store.insert_open_feedback(trigger="inline", category="coverage_gap",
                                         verbatim="no ohio medicaid")
        assert fid and len(fid) == 36  # uuid4

    def test_survey_insert_success(self, monkeypatch):
        monkeypatch.setattr(store, "db_execute", lambda *a, **k: {"rows_affected": 1})
        fid = store.insert_survey_score(survey_type="nps", score=9, user_id="u1")
        assert fid

    def test_routing_table(self):
        assert store.route_for("bug") == "triage_queue"
        assert store.route_for("coverage_gap") == "corpus_backlog"
        assert store.route_for("praise") == "none"

    def test_docs_gap_routes_to_docs_backlog(self):
        # product-awareness integration: a doc-miss is filed as docs_gap and must
        # route to docs_backlog (docs/product-awareness-feedback-contract.md).
        assert store.route_for("docs_gap") == "docs_backlog"
        assert "docs_gap" in store.ROUTING

    def test_doc_stale_routes_to_docs_refresh(self):
        # supply side of doc freshness: a builder ships a change → doc_stale →
        # docs_refresh (drained by the weekly sweep).
        assert store.route_for("doc_stale") == "docs_refresh"
        assert "doc_stale" in store.ROUTING

    def test_close_signals_dev_returns_zero_on_db_down(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "dev")
        monkeypatch.setattr(store, "db_execute",
                            lambda *a, **k: {"error": {"code": "connection_error",
                                                       "message": "down"}})
        assert store.close_signals(category="doc_stale", module="chat") == 0

    def test_close_signals_returns_rows_affected(self, monkeypatch):
        monkeypatch.setattr(store, "db_execute", lambda *a, **k: {"rows_affected": 3})
        assert store.close_signals(category="doc_stale") == 3

    def test_module_slug_vocabulary_is_the_shared_set(self):
        # area_tag == module; canonical conceptual slugs shared with product-awareness.
        assert set(store.MODULE_SLUGS) == {
            "chat", "rag", "lexicon", "skills", "strategy",
            "os", "credentialing", "roster", "auth", "document-viewer", "infra",
        }


# ── skill handler ───────────────────────────────────────────────────────────

class TestSkillHandler:
    @pytest.fixture
    def _skill(self):
        import app.skills.builtin.product_feedback as pf
        return pf

    def _call(self, inputs):
        return SkillCall(name="product_feedback", inputs=inputs,
                         question=inputs.get("verbatim", ""),
                         user_message=inputs.get("verbatim", ""), thread_id="t1")

    def test_open_feedback_classifies_persists_and_acks(self, monkeypatch, _skill):
        monkeypatch.setattr(_skill, "_classify", lambda **k: {
            "classification": {"category": "coverage_gap", "sentiment": "negative",
                               "severity": "high", "summary": "no ohio",
                               "tidied": "Ohio Medicaid is missing."},
            "routed_to": "corpus_backlog",
        })
        monkeypatch.setattr(_skill.store, "insert_open_feedback", lambda **k: "fid-123")
        monkeypatch.setattr(_skill.store, "log_event", lambda **k: None)
        monkeypatch.setattr(_skill.store, "mark_captured", lambda *a, **k: None)

        env = _skill._run_product_feedback(self._call(
            {"trigger": "inline", "verbatim": "you never have ohio medicaid"}))
        assert "coverage" in env.text.lower()
        assert env.extra["feedback_id"] == "fid-123"
        assert env.extra["category"] == "coverage_gap"
        assert env.extra["capture_card"]["editable"] is True

    def test_empty_verbatim_returns_empty(self, _skill):
        env = _skill._run_product_feedback(self._call({"trigger": "on_demand"}))
        assert env.text == ""

    def test_survey_path(self, monkeypatch, _skill):
        monkeypatch.setattr(_skill.store, "insert_survey_score", lambda **k: "sfid")
        monkeypatch.setattr(_skill.store, "log_event", lambda **k: None)
        monkeypatch.setattr(_skill.store, "mark_captured", lambda *a, **k: None)
        env = _skill._run_product_feedback(self._call(
            {"trigger": "periodic", "kind": "survey", "survey_type": "nps", "score": 9}))
        assert env.extra["survey_type"] == "nps"
        assert env.extra["score"] == 9

    def test_skill_is_in_planner_manifest(self):
        """Registration alone isn't enough — the manifest is an explicit list,
        so the skill must be named in tool_manifest or the planner never sees
        it (the 2026-07-02 'planner hand-acknowledged instead of calling it' bug)."""
        from app.pipeline import tool_manifest as tm
        manifest = tm.get_tool_manifest()
        assert "product_feedback(" in manifest

    def test_classify_service_down_still_persists(self, monkeypatch, _skill):
        # _classify's own fallback returns a best-effort dict; handler must still write
        monkeypatch.setattr(_skill.store, "insert_open_feedback", lambda **k: "fid-x")
        monkeypatch.setattr(_skill.store, "log_event", lambda **k: None)
        monkeypatch.setattr(_skill.store, "mark_captured", lambda *a, **k: None)
        # force the real _classify to hit its except branch by pointing at a dead URL
        monkeypatch.setattr(_skill, "FEEDBACK_SKILL_URL", "http://127.0.0.1:1/classify")
        env = _skill._run_product_feedback(self._call(
            {"trigger": "on_demand", "verbatim": "the app is too slow",
             "category": "speed"}))
        assert env.extra["feedback_id"] == "fid-x"
        assert env.extra["category"] == "speed"  # provisional preserved on fallback
