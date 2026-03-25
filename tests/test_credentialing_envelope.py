"""Unit tests for credentialing_envelope helpers and run_pipeline refined_query wiring."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.pipeline.credentialing_envelope import (
    build_canonical_credentialing_message,
    classify_org_vs_uploads,
    envelope_routes_to_reconciliation,
)
from app.pipeline.orchestrator import run_pipeline


def test_build_canonical_outside_in_credentialing():
    merged = {"active": {}}
    co = {"org_name": "Acme Health", "report_kind": "auto"}
    assert build_canonical_credentialing_message("ignored", merged, co) == "Create a credentialing report for Acme Health."


def test_build_canonical_reconciliation_when_roster_on_thread():
    merged = {
        "active": {
            "reconciliation_upload_id": "u1",
            "reconciliation_org_id": "1234567890",
            "uploaded_files": [
                {
                    "purpose": "roster_reconciliation",
                    "upload_id": "u1",
                    "org_id": "1234567890",
                    "org_name": "Acme Health",
                }
            ],
        }
    }
    co = {"org_name": "Acme Health", "report_kind": "auto"}
    assert build_canonical_credentialing_message("x", merged, co) == "Run roster reconciliation report for Acme Health."


def test_classify_org_vs_uploads_matched():
    active = {
        "uploaded_files": [
            {"purpose": "roster_reconciliation", "org_name": "David Lawrence Center"},
        ]
    }
    assert classify_org_vs_uploads("david lawrence", active) == "matched"


def test_classify_org_vs_uploads_ambiguous():
    active = {
        "uploaded_files": [
            {"purpose": "roster_reconciliation", "org_name": "Other Org"},
        ]
    }
    assert classify_org_vs_uploads("Acme", active) == "ambiguous"


def test_envelope_routes_to_reconciliation_respects_prefer_outside_in():
    merged = {
        "active": {
            "reconciliation_upload_id": "u1",
            "reconciliation_org_id": "1",
        }
    }
    assert envelope_routes_to_reconciliation(merged, {"prefer_outside_in": True, "report_kind": "auto"}, "") is False


def test_run_pipeline_sets_refined_query_when_credentialing_options(monkeypatch):
    """After state_load, canonical string is ctx.refined_query and ctx.message."""
    seen: list = []

    def fake_state_load(ctx):
        ctx.merged_state = {"active": {}}

    def fake_run_react(ctx, emitter=None):
        seen.append((ctx.refined_query, ctx.message))

    def fake_run_integrate(ctx, emitter=None):
        ctx.response_payload = {"status": "completed", "message": "ok", "sources": []}
        ctx.final_message = "ok"

    monkeypatch.setattr("app.pipeline.orchestrator.run_state_load", fake_state_load)
    monkeypatch.setattr("app.pipeline.react_loop.run_react", fake_run_react)
    monkeypatch.setattr("app.pipeline.orchestrator.run_integrate", fake_run_integrate)
    monkeypatch.setattr("app.pipeline.orchestrator._publish_completed", lambda ctx, t0: None)
    monkeypatch.setattr("app.pipeline.orchestrator.start_progress", lambda x: None)
    monkeypatch.setattr("app.pipeline.orchestrator.clear_progress", lambda x: None)
    monkeypatch.setattr("app.pipeline.orchestrator.get_queue", MagicMock())
    monkeypatch.setattr("app.pipeline.orchestrator.store_response", lambda *a, **k: None)
    monkeypatch.setattr("app.services.post_run_adjudication.schedule_post_run_adjudication", lambda *a, **k: None)

    run_pipeline(
        "cid-env",
        "Create a credentialing report for Acme Health",
        None,
        credentialing_options={"org_name": "Acme Health", "mode": "autopilot", "force_refresh": False},
    )
    assert len(seen) == 1
    rq, msg = seen[0]
    assert rq == "Create a credentialing report for Acme Health."
    assert msg == "Create a credentialing report for Acme Health."
