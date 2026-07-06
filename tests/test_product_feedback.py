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

    def test_close_signals_dev_returns_empty_on_db_down(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "dev")
        monkeypatch.setattr(store, "db_query",
                            lambda *a, **k: {"error": {"code": "connection_error",
                                                       "message": "down"}})
        assert store.close_signals(category="doc_stale", module="chat") == []

    def test_close_signals_returns_closed_ids(self, monkeypatch):
        # SELECT the matching ids, then UPDATE — returns the drained ids (audit trail)
        monkeypatch.setattr(store, "db_query",
                            lambda *a, **k: {"rows": [["id-1"], ["id-2"], ["id-3"]]})
        monkeypatch.setattr(store, "db_execute", lambda *a, **k: {"rows_affected": 3})
        assert store.close_signals(category="doc_stale", module="chat") == ["id-1", "id-2", "id-3"]

    def test_close_signals_empty_when_nothing_matches(self, monkeypatch):
        monkeypatch.setattr(store, "db_query", lambda *a, **k: {"rows": []})
        # db_execute must not even be called when there's nothing to close
        monkeypatch.setattr(store, "db_execute",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not UPDATE")))
        assert store.close_signals(category="doc_stale", module="chat") == []

    def test_module_slug_vocabulary_is_the_shared_set(self):
        # area_tag == module; canonical conceptual slugs shared with product-awareness.
        assert set(store.MODULE_SLUGS) == {
            "chat", "rag", "lexicon", "skills", "strategy", "eval",
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

    def test_open_returns_playback_receipt(self, monkeypatch, _skill):
        monkeypatch.setattr(_skill, "_classify", lambda **k: {
            "classification": {"category": "usability", "sentiment": "negative",
                               "severity": "medium", "summary": "s",
                               "tidied": "The sidebar is confusing."},
            "routed_to": "product_backlog"})
        monkeypatch.setattr(_skill.store, "insert_open_feedback", lambda **k: "fid")
        monkeypatch.setattr(_skill.store, "log_event", lambda **k: None)
        monkeypatch.setattr(_skill.store, "mark_captured", lambda *a, **k: None)
        env = _skill._run_product_feedback(self._call(
            {"trigger": "on_demand", "verbatim": "the sidebar is confusing"}))
        assert "thank you" in env.text.lower()
        assert "Usability" in env.text
        assert "update it" in env.text.lower()          # invites editing
        assert env.extra["capture_card"]["editable"] is True

    def test_update_path_edits_existing_item(self, monkeypatch, _skill):
        seen = {}
        monkeypatch.setattr(_skill.store, "update_open_feedback",
                            lambda **k: seen.update(k) or {"feedback_id": "fid", "category": "bug",
                                                            "tidied": "X. Happens on mobile.",
                                                            "routed_to": "triage_queue"})
        env = _skill._run_product_feedback(self._call(
            {"update": True, "category": "bug", "add_detail": "Happens on mobile."}))
        assert seen["category"] == "bug" and seen["add_detail"] == "Happens on mobile."
        assert "updated" in env.text.lower()
        assert "Bug" in env.text
        assert env.extra["feedback_id"] == "fid"

    def test_update_not_found_asks_to_restate(self, monkeypatch, _skill):
        monkeypatch.setattr(_skill.store, "update_open_feedback", lambda **k: None)
        env = _skill._run_product_feedback(self._call({"update": True, "category": "bug"}))
        assert "couldn't find" in env.text.lower()

    def test_survey_returns_receipt_with_thanks(self, monkeypatch, _skill):
        monkeypatch.setattr(_skill.store, "insert_survey_score", lambda **k: "sfid")
        monkeypatch.setattr(_skill.store, "log_event", lambda **k: None)
        monkeypatch.setattr(_skill.store, "mark_captured", lambda *a, **k: None)
        env = _skill._run_product_feedback(self._call(
            {"trigger": "periodic", "kind": "survey", "survey_type": "nps", "score": 9}))
        assert "9/10" in env.text and "thanks" in env.text.lower()

    def test_promotion_policy(self, _skill):
        f = _skill._promotion_severity
        assert f("bug", "high") == "critical"
        assert f("bug", "low") == "warning"
        assert f("accuracy_trust", "medium") == "warning"
        assert f("usability", "high") == "warning"
        assert f("usability", "medium") is None   # mild non-bug → no page
        assert f("speed", "low") is None
        assert f("praise", "high") is None         # praise never pages

    def test_page_worthy_feedback_promotes_a_task(self, monkeypatch, _skill):
        monkeypatch.setattr(_skill, "_classify", lambda **k: {
            "classification": {"category": "bug", "sentiment": "negative",
                               "severity": "high", "summary": "export crashes",
                               "tidied": "Exporting the roster crashes the app."},
            "routed_to": "triage_queue"})
        monkeypatch.setattr(_skill.store, "insert_open_feedback", lambda **k: "fid-bug")
        monkeypatch.setattr(_skill.store, "log_event", lambda **k: None)
        monkeypatch.setattr(_skill.store, "mark_captured", lambda *a, **k: None)
        captured = {}
        # promote is imported lazily inside _maybe_promote_task
        import app.services.task_manager_promotion as tmp
        monkeypatch.setattr(tmp, "promote", lambda env: captured.update(env))
        _skill._run_product_feedback(self._call(
            {"trigger": "on_demand", "verbatim": "exporting the roster crashes the app"}))
        assert captured.get("report_to_task_manager") is True
        assert captured.get("source_module") == "feedback"
        assert captured.get("task_severity") == "critical"
        assert captured["data"]["feedback_id"] == "fid-bug"
        # dedup fix: stable per-item source_ref, not a NULL-collapsing correlation_id
        assert captured.get("source_ref") == "feedback:fid-bug"
        # readable title (not the bare category string) + categorized issue_code
        assert "export crashes" in captured.get("title", "")
        assert captured.get("issue_code") == "bug"
        assert captured.get("org_name") == "_shared_"    # sentinel: no org on this feedback

    def test_build_signal_body_prefers_explicit_source_ref_and_forwards_title(self):
        from app.services.task_manager_promotion import _build_signal_body
        body = _build_signal_body({
            "signal": "product_feedback", "correlation_id": "cid-123",
            "source_ref": "feedback:abc", "title": "Feedback (bug): x",
            "issue_code": "bug", "task_type": "blocker", "task_severity": "warning",
        })
        assert body["source_ref"] == "feedback:abc"      # explicit wins over correlation_id
        assert body["title"] == "Feedback (bug): x"
        assert body["issue_code"] == "bug"
        # backward-compat: no explicit source_ref → still derives from correlation_id
        legacy = _build_signal_body({"correlation_id": "cid-9", "task_type": "blocker"})
        assert legacy["source_ref"] == "correlation_id:cid-9"
        assert legacy["title"] is None

    def test_mild_feedback_does_not_promote(self, monkeypatch, _skill):
        monkeypatch.setattr(_skill, "_classify", lambda **k: {
            "classification": {"category": "usability", "sentiment": "neutral",
                               "severity": "low", "summary": "x", "tidied": "y"},
            "routed_to": "product_backlog"})
        monkeypatch.setattr(_skill.store, "insert_open_feedback", lambda **k: "fid-u")
        monkeypatch.setattr(_skill.store, "log_event", lambda **k: None)
        monkeypatch.setattr(_skill.store, "mark_captured", lambda *a, **k: None)
        called = []
        import app.services.task_manager_promotion as tmp
        monkeypatch.setattr(tmp, "promote", lambda env: called.append(env))
        _skill._run_product_feedback(self._call(
            {"trigger": "on_demand", "verbatim": "the spacing feels a little tight"}))
        assert called == []   # mild usability → no page

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

    def test_agent_source_sets_user_and_skips_cadence(self, monkeypatch):
        # external agent posts doc_stale via HTTP: source → user_id, no cadence pollution
        import app.api.product_feedback as api
        captured = {}
        monkeypatch.setattr(api.store, "insert_open_feedback", lambda **k: captured.update(k) or "fid")
        logged = []
        monkeypatch.setattr(api.store, "log_event", lambda **k: logged.append(k))
        marked = []
        monkeypatch.setattr(api.store, "mark_captured", lambda uid: marked.append(uid))
        body = api.OpenFeedbackBody(verbatim="renamed sidebar", category="doc_stale",
                                    trigger="agent_signal", area_tags=["chat"], source="agent:chat")
        r = api.post_product_feedback(body, user_id="real-user-123")
        assert captured["user_id"] == "agent:chat"   # source wins for provenance
        assert marked == []                            # user cadence NOT advanced
        assert logged == []                            # not in the user funnel either
        assert r["routed_to"] == "docs_refresh"

    def test_update_endpoint_reroutes_and_returns_row(self, monkeypatch):
        import app.api.product_feedback as api
        seen = {}
        monkeypatch.setattr(api.store, "update_open_feedback",
                            lambda **k: seen.update(k) or {"feedback_id": "f", "category": "bug",
                                                            "tidied": "X", "routed_to": "triage_queue"})
        body = api.UpdateFeedbackBody(feedback_id="f", category="bug", tidied="X")
        r = api.post_update_feedback(body, _user_id="u")
        assert seen["category"] == "bug" and seen["tidied"] == "X"
        assert r["routed_to"] == "triage_queue"

    def test_update_endpoint_404_when_missing(self, monkeypatch):
        import app.api.product_feedback as api
        from fastapi import HTTPException
        monkeypatch.setattr(api.store, "update_open_feedback", lambda **k: None)
        try:
            api.post_update_feedback(api.UpdateFeedbackBody(feedback_id="nope"), _user_id="u")
            assert False, "expected 404"
        except HTTPException as e:
            assert e.status_code == 404

    def test_close_signals_endpoint_drains(self, monkeypatch):
        import app.api.product_feedback as api
        seen = {}
        monkeypatch.setattr(api.store, "close_signals",
                            lambda **k: seen.update(k) or ["id-a", "id-b", "id-c"])
        r = api.post_close_signals(
            api.CloseSignalsBody(category="doc_stale", module="chat",
                                 before="2026-07-06T00:00:00Z"), _user_id="agent")
        assert r["drained"] == 3 and r["feedback_ids"] == ["id-a", "id-b", "id-c"]
        assert seen["category"] == "doc_stale" and seen["module"] == "chat"
        assert seen["before"] == "2026-07-06T00:00:00Z"

    def test_close_signals_rejects_user_feedback_categories(self):
        import app.api.product_feedback as api
        from fastapi import HTTPException
        # closing user feedback (bug/usability) via the sweep endpoint is refused
        for cat in ("bug", "usability", "praise"):
            try:
                api.post_close_signals(api.CloseSignalsBody(category=cat, module="chat"),
                                       _user_id="agent")
                assert False, f"{cat} should be rejected"
            except HTTPException as e:
                assert e.status_code == 400

    def test_close_signals_requires_module(self):
        import app.api.product_feedback as api
        from fastapi import HTTPException
        try:
            api.post_close_signals(api.CloseSignalsBody(category="doc_stale", module="  "),
                                   _user_id="agent")
            assert False, "empty module should be rejected"
        except HTTPException as e:
            assert e.status_code == 400

    def test_enrich_offer_feedback_shapes(self):
        from app.pipeline.react.feedback_signal import enrich_offer_feedback
        nps = enrich_offer_feedback({"kind": "nps", "trigger": "periodic"})
        assert nps["scale"] == {"min": 0, "max": 10, "min_label": "Not likely", "max_label": "Very likely"}
        assert nps["post_to"] == "/chat/product-feedback/score"
        csat = enrich_offer_feedback({"kind": "csat"})
        assert csat["scale"]["max"] == 5
        generic = enrich_offer_feedback({"kind": "generic"})
        assert generic["cta"] == "Share feedback" and generic["post_to"] == "/chat/product-feedback"

    def test_user_feedback_advances_cadence(self, monkeypatch):
        import app.api.product_feedback as api
        monkeypatch.setattr(api.store, "insert_open_feedback", lambda **k: "fid")
        monkeypatch.setattr(api.store, "log_event", lambda **k: None)
        marked = []
        monkeypatch.setattr(api.store, "mark_captured", lambda uid: marked.append(uid))
        body = api.OpenFeedbackBody(verbatim="the sidebar is confusing", category="usability")
        api.post_product_feedback(body, user_id="real-user-123")
        assert marked == ["real-user-123"]   # genuine user feedback → cadence advances

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
