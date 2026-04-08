"""Tests for credentialing report force-reload: 'reload and create credentialing report' runs daily load first.
Also: first-of-day auto-reload and same-day cache serving."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.services.tool_agent import (
    answer_tool,
    _run_fl_medicaid_daily_load,
    _ensure_bq_env_for_daily_load,
    _clean_org_name_for_credentialing,
    _should_run_first_of_day_reload,
)
from app.services.roster_credentialing_orchestrator import _step_num


# ---------------------------------------------------------------------------
# _run_fl_medicaid_daily_load: skip when script not found
# ---------------------------------------------------------------------------

def test_run_fl_medicaid_daily_load_skips_when_dir_missing():
    """When MOBIUS_DBT_DIR points to a path with no script, emitter receives Reload skipped."""
    emitted: list[str] = []
    with patch.dict(os.environ, {"MOBIUS_DBT_DIR": "/nonexistent/path"}, clear=False):
        _run_fl_medicaid_daily_load(emitter=emitted.append)
    assert len(emitted) >= 1
    assert any("Reload skipped" in m for m in emitted)




# ---------------------------------------------------------------------------
# Reload trigger + org extraction: "reload and create credentialing report for X"
# ---------------------------------------------------------------------------

def test_reload_and_create_credentialing_report_emits_reload_then_report():
    """'Reload and create credentialing report for Aspire Health' triggers reload step then report."""
    emitted: list[str] = []
    extra_out: dict = {}

    with patch.dict(os.environ, {"MOBIUS_DBT_DIR": "/nonexistent"}, clear=False):
        with patch("app.services.tool_agent._get_latest_run_for_org", return_value=None):
            with patch(
                "app.services.tool_agent.run_orchestrator",
                return_value=("Report generated.", MagicMock(step_outputs=[], report_final_md="", report_pdf_base64="", report_run_id="")),
            ):
                answer_tool(
                    question="reload and create credentialing report for Aspire Health",
                    user_message="reload and create credentialing report for Aspire Health",
                    emitter=emitted.append,
                    extra_out=extra_out,
                )

    # Reload step ran first (skip message when script missing)
    assert any("Reload skipped" in m for m in emitted), f"Expected reload message in: {emitted}"
    # Then report step
    assert any("Medicaid NPI report" in m and "Aspire Health" in m for m in emitted), f"Expected report message in: {emitted}"


def test_create_credentialing_report_without_reload_does_not_emit_reload():
    """'Create credentialing report for Aspire Health' (no reload phrase, not first-of-day) does not emit Reload."""
    emitted: list[str] = []
    with patch("app.services.tool_agent._should_run_first_of_day_reload", return_value=False):
        with patch("app.services.tool_agent._get_latest_run_for_org", return_value=None):
            with patch(
                "app.services.tool_agent.run_orchestrator",
                return_value=("Report generated.", MagicMock(step_outputs=[], report_final_md="", report_pdf_base64="", report_run_id="")),
            ):
                answer_tool(
                    question="create credentialing report for Aspire Health",
                    user_message="create credentialing report for Aspire Health",
                    emitter=emitted.append,
                    extra_out={},
                )
    assert not any("Reload" in m for m in emitted), f"Should not emit reload when user did not ask for reload: {emitted}"


def test_clean_org_name_reload_phrase_unchanged():
    """Org name extracted after 'reload and create credentialing report for' is cleaned correctly."""
    # Simulate what we extract: "Aspire Health" (after "create credentialing report for")
    name = "Aspire Health"
    assert _clean_org_name_for_credentialing(name) == "Aspire Health"
    # With jurisdiction suffix
    assert "Aspire" in _clean_org_name_for_credentialing("Aspire Health in Florida")


# ---------------------------------------------------------------------------
# BQ_* env loading for dbt (so daily load doesn't skip dbt)
# ---------------------------------------------------------------------------

def test_ensure_bq_env_loads_from_example():
    """When BQ_* are not set, _ensure_bq_env_for_daily_load loads them from mobius-chat/.env.example."""
    for k in ("BQ_PROJECT", "BQ_LANDING_MEDICAID_DATASET", "BQ_MARTS_MEDICAID_DATASET"):
        os.environ.pop(k, None)
    _ensure_bq_env_for_daily_load()
    assert os.environ.get("BQ_PROJECT") == "mobius-os-dev"
    assert os.environ.get("BQ_LANDING_MEDICAID_DATASET") == "landing_medicaid_npi_dev"
    assert os.environ.get("BQ_MARTS_MEDICAID_DATASET") == "mobius_medicaid_npi_dev"


# ---------------------------------------------------------------------------
# Step numbering: locations = Step 3, associated providers = Step 4
# ---------------------------------------------------------------------------

def test_step_num_locations_and_associated_providers():
    """ensure_benchmarks is Step 1; identify_org Step 2; find_locations Step 3; find_associated_providers Step 6."""
    assert _step_num("ensure_benchmarks") == 1
    assert _step_num("identify_org") == 2
    assert _step_num("find_locations") == 3
    assert _step_num("find_associated_providers") == 6
    assert _step_num("build_report") == 16


# ---------------------------------------------------------------------------
# First-of-day reload and same-day cache
# ---------------------------------------------------------------------------

def test_first_of_day_triggers_reload():
    """First NPI report of the day triggers FL Medicaid reload before building."""
    emitted: list[str] = []
    with patch("app.services.tool_agent._should_run_first_of_day_reload", return_value=True):
        with patch.dict(os.environ, {"MOBIUS_DBT_DIR": "/nonexistent"}, clear=False):
            with patch("app.services.tool_agent._get_latest_run_for_org", return_value=None):
                with patch(
                    "app.services.tool_agent.run_orchestrator",
                    return_value=("Report generated.", MagicMock(step_outputs=[], report_run_id="")),
                ):
                    answer_tool(
                        question="create credentialing report for Aspire Health",
                        user_message="create credentialing report for Aspire Health",
                        emitter=emitted.append,
                        extra_out={},
                    )
    assert any("Reload" in m for m in emitted), f"First of day should trigger reload: {emitted}"


def test_same_day_report_served_from_cache():
    """Subsequent same-day report for same org returns cached report, no full chain."""
    emitted: list[str] = []
    extra_out: dict = {}
    from datetime import datetime, timezone
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d") + "T12:00:00+00:00"
    cached_run = {
        "report_run_id": "cached-run-123",
        "org_name": "Aspire Health",
        "status": "completed",
        "created_at": today_iso,
        "step_outputs": [],
        "documents": {"final_md": "# Cached Report\n\nContent from earlier today."},
    }
    with patch("app.services.tool_agent._should_run_first_of_day_reload", return_value=False):
        with patch("app.services.tool_agent._get_latest_run_for_org", return_value=cached_run):
            out = answer_tool(
                question="create credentialing report for Aspire Health",
                user_message="create credentialing report for Aspire Health",
                emitter=emitted.append,
                extra_out=extra_out,
            )
    assert any("cached" in m.lower() for m in emitted), f"Should emit cached: {emitted}"
    assert extra_out.get("report_run_id") == "cached-run-123"
    assert "Cached Report" in (out[0] or "")
