"""Tests for product_help_search: HTTP service + feedback store mocked.

Verifies the 3 outcomes, the best-effort in-process gap write (docs/product-awareness-
feedback-contract.md), graceful degradation, and manifest presence (the explicit-list
gotcha). No real service or DB is touched.
"""
from __future__ import annotations

import app.skills.builtin.product_help_search as ph
import app.storage.product_feedback as store
from app.skills.registry import SkillCall


def _call(inputs, thread_id="t1"):
    return SkillCall(name="product_help_search", inputs=inputs,
                     question=inputs.get("query", ""),
                     user_message=inputs.get("query", ""), thread_id=thread_id)


def test_answer_outcome_returns_sources(monkeypatch):
    monkeypatch.setattr(ph, "_search", lambda payload: {
        "outcome": "answer", "text": "To sign in: click Google.",
        "module": "auth", "s_top": 0.62,
        "sources": [{"chunk_id": "auth:capabilities:0", "module": "auth",
                     "section": "Capabilities", "doc_type": "reference",
                     "source_path": "docs/product-docs/user-and-auth.md", "score": 0.62}],
        "gap": None})
    env = ph._run_product_help(_call({"query": "how do I sign in"}))
    assert env.signal == "corpus_only"
    assert env.extra["outcome"] == "answer"
    assert len(env.sources) == 1 and env.sources[0].document_id == "auth:capabilities:0"
    assert env.extra["feedback_id"] is None


def test_docs_gap_files_feedback(monkeypatch):
    recorded = {}
    monkeypatch.setattr(store, "insert_open_feedback", lambda **k: (recorded.update(k) or "fid-gap"))
    monkeypatch.setattr(store, "route_for", lambda c: "docs_backlog" if c == "docs_gap" else "product_backlog")
    monkeypatch.setattr(ph, "_search", lambda payload: {
        "outcome": "docs_gap", "text": "I don't have documentation on that yet.",
        "module": "chat", "s_top": 0.05, "sources": [],
        "gap": {"category": "docs_gap", "module": "chat",
                "verbatim": "how do I export data", "summary": "no doc for: how do I export data"}})
    env = ph._run_product_help(_call({"query": "how do I export data"}))
    assert env.signal == "no_sources" and env.extra["outcome"] == "docs_gap"
    assert env.extra["feedback_id"] == "fid-gap"
    assert recorded["category"] == "docs_gap"
    assert recorded["routed_to"] == "docs_backlog"
    assert recorded["area_tags"] == ["chat"]
    assert recorded["trigger"] == "auto_harvest"    # machine-harvested, distinct from user feedback
    assert recorded["verbatim"] == "how do I export data"


def test_feature_request_files_feedback(monkeypatch):
    recorded = {}
    monkeypatch.setattr(store, "insert_open_feedback", lambda **k: (recorded.update(k) or "fid-fr"))
    monkeypatch.setattr(store, "route_for", lambda c: "product_backlog")
    monkeypatch.setattr(ph, "_search", lambda payload: {
        "outcome": "feature_request", "text": "That's planned but not available yet.",
        "module": "auth", "s_top": 0.4, "sources": [],
        "gap": {"category": "feature_request", "module": "auth",
                "verbatim": "invite my team", "summary": "asked for planned capability"}})
    env = ph._run_product_help(_call({"query": "invite my team"}))
    assert env.extra["outcome"] == "feature_request"
    assert recorded["category"] == "feature_request" and recorded["routed_to"] == "product_backlog"
    assert env.extra["feedback_id"] == "fid-fr"


def test_gap_write_failure_never_breaks_answer(monkeypatch):
    def _boom(**k):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "insert_open_feedback", _boom)
    monkeypatch.setattr(store, "route_for", lambda c: "docs_backlog")
    monkeypatch.setattr(ph, "_search", lambda payload: {
        "outcome": "docs_gap", "text": "no docs", "module": "chat", "s_top": 0.05, "sources": [],
        "gap": {"category": "docs_gap", "module": "chat", "verbatim": "x", "summary": "y"}})
    env = ph._run_product_help(_call({"query": "x"}))   # must NOT raise
    assert env.extra["outcome"] == "docs_gap" and env.extra["feedback_id"] is None


def test_service_down_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(ph, "_search", lambda payload: None)
    env = ph._run_product_help(_call({"query": "anything"}))
    assert env.signal == "no_sources" and env.sources == []


def test_unknown_module_not_tagged(monkeypatch):
    recorded = {}
    monkeypatch.setattr(store, "insert_open_feedback", lambda **k: (recorded.update(k) or "fid"))
    monkeypatch.setattr(store, "route_for", lambda c: "docs_backlog")
    monkeypatch.setattr(ph, "_search", lambda payload: {
        "outcome": "docs_gap", "text": "...", "module": "unknown", "s_top": 0.0, "sources": [],
        "gap": {"category": "docs_gap", "module": "unknown", "verbatim": "q", "summary": "s"}})
    ph._run_product_help(_call({"query": "q"}))
    assert recorded["area_tags"] is None     # unknown module → no area_tag


def test_empty_query_returns_empty():
    assert ph._run_product_help(_call({})).text == ""


def test_skill_in_planner_manifest():
    from app.pipeline import tool_manifest as tm
    assert "product_help_search(" in tm.get_tool_manifest()
