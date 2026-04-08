"""POST /chat ChatRequest parsing and credentialing_options propagation."""
from __future__ import annotations

import pytest

from app.main import ChatRequest, CredentialingOptions


def test_chat_request_optional_fields_default():
    body = ChatRequest.model_validate({"message": "hi"})
    assert body.message == "hi"
    assert body.thread_id is None
    assert body.credentialing_options is None
    assert body.use_react is None


def test_chat_request_credentialing_options_roundtrip():
    body = ChatRequest.model_validate(
        {
            "message": "Create a credentialing report for Test Org",
            "thread_id": "t1",
            "credentialing_options": {
                "org_name": "Test Org",
                "mode": "copilot",
                "force_refresh": True,
            },
            "use_react": True,
        }
    )
    assert body.credentialing_options is not None
    assert body.credentialing_options.org_name == "Test Org"
    assert body.credentialing_options.mode == "copilot"
    assert body.credentialing_options.force_refresh is True
    dumped = body.credentialing_options.model_dump(exclude_none=True)
    assert dumped == {"org_name": "Test Org", "mode": "copilot", "force_refresh": True}


def test_credentialing_options_model_accepts_partial():
    co = CredentialingOptions.model_validate({"org_name": "Acme"})
    assert co.org_name == "Acme"
    assert co.mode is None
    assert co.force_refresh is None
    assert co.report_kind is None
    assert co.prefer_outside_in is None


def test_credentialing_options_reconciliation_fields():
    co = CredentialingOptions.model_validate(
        {"org_name": "X", "report_kind": "reconciliation", "prefer_outside_in": False}
    )
    assert co.report_kind == "reconciliation"
    assert co.prefer_outside_in is False


def test_credentialing_options_prefer_fresh_report_roundtrip():
    body = ChatRequest.model_validate(
        {
            "message": "x",
            "credentialing_options": {
                "org_name": "Acme",
                "mode": "autopilot",
                "force_refresh": False,
                "prefer_fresh_report": True,
            },
        }
    )
    assert body.credentialing_options is not None
    assert body.credentialing_options.prefer_fresh_report is True
    dumped = body.credentialing_options.model_dump(exclude_none=True)
    assert dumped.get("prefer_fresh_report") is True


def test_worker_process_one_passes_credentialing_into_run_pipeline(monkeypatch):
    """process_one passes credentialing_options and use_react into run_pipeline."""
    from app.worker import run as worker_run

    captured: dict = {}

    def fake_run_pipeline(cid, message, thread_id, t0_start=None, credentialing_options=None, use_react_override=None, chat_mode=None):
        captured["credentialing_options"] = credentialing_options
        captured["use_react_override"] = use_react_override

    monkeypatch.setattr("app.pipeline.orchestrator.run_pipeline", fake_run_pipeline)
    worker_run.process_one(
        "cid",
        {
            "message": "Create a credentialing report for Foo",
            "thread_id": "tid",
            "credentialing_options": {"org_name": "Foo", "mode": "autopilot", "force_refresh": True},
            "use_react": False,
        },
    )
    assert captured["credentialing_options"]["org_name"] == "Foo"
    assert captured["credentialing_options"]["force_refresh"] is True
    assert captured["use_react_override"] is False
