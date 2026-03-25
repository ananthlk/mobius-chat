"""
Comprehensive credentialing / reconciliation tests.

Default (CI): mocks only — no real Vertex, no provider-roster HTTP, no LLM report generation.

Optional real stack: MOBIUS_RUN_CREDENTIALING_INTEGRATION=1 + CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL +
  working provider-roster-credentialing + ADC/Vertex (heavy; run locally).

Run mocked suite:
  uv run pytest tests/test_credentialing_reports_comprehensive.py -q

Skip integration unless env is set:
  MOBIUS_RUN_CREDENTIALING_INTEGRATION=1 uv run pytest tests/test_credentialing_reports_comprehensive.py -m credentialing_integration -q
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.planner.blueprint import build_blueprint
from app.planner.credentialing_flow_intent import parse_credentialing_flow_intent
from app.planner.schemas import Plan, SubQuestion
from app.pipeline.context import PipelineContext
from app.pipeline.react_loop import _execute_tool
from app.services.doc_assembly import RETRIEVAL_SIGNAL_ROSTER_COMPLETE
from app.services.roster_credentialing_orchestrator import (
    ROSTER_CREDENTIALING_PLAN,
    OrchestratorState,
    StepOutput,
    StepState,
)
from app.services.tool_agent import answer_tool


def _plan_one(text: str, **sq_kwargs) -> Plan:
    return Plan(
        subquestions=[
            SubQuestion(
                id="sq1",
                text=text,
                kind="non_patient",
                question_intent="factual",
                intent_score=0.75,
                **sq_kwargs,
            )
        ],
        credentialing_flow_intent=parse_credentialing_flow_intent(text),
    )


@pytest.mark.parametrize(
    "message,sq_tool_hint,expected_tool_hint",
    [
        # Planner already chose roster_report; flow intent does not override outside-in build
        ("Create a credentialing report for Synthetic Org LLC", "roster_report", "roster_report"),
        ("Run a reconciliation report for Synthetic Org LLC", None, "roster_reconciliation"),
        ("List my previous roster uploads on this chat", None, "list_thread_document_uploads"),
    ],
)
def test_blueprint_tool_hint_for_credentialing_flows(
    message: str, sq_tool_hint: str | None, expected_tool_hint: str
) -> None:
    """Deterministic route + credentialing_flow_intent steer first subquestion tool_hint."""
    kw = {"tool_hint": sq_tool_hint} if sq_tool_hint else {}
    plan = _plan_one(message, **kw)
    bp = build_blueprint(plan, retrieval_ctx={"user_message": message})
    assert bp[0]["agent"] == "tool"
    assert bp[0]["tool_hint"] == expected_tool_hint


def _synthetic_ostate(org_name: str = "Synthetic Org LLC") -> OrchestratorState:
    steps = [StepState(id=s["id"], label=s["label"]) for s in ROSTER_CREDENTIALING_PLAN]
    body = "## Executive Summary\n\nSynthetic credentialing report for testing.\n\n" + ("x" * 400)
    st = OrchestratorState(steps=steps, org_npis=["1234567890"], org_name=org_name)
    st.step_outputs = [
        StepOutput(step_id="opportunity_sizing", label="Opportunity sizing", csv_content="k,v\na,1", row_count=1),
        StepOutput(step_id="build_report", label="Final report", csv_content="(markdown in report_final_md)", row_count=1),
    ]
    st.report_final_md = body
    st.report_run_id = "test-run-synthetic"
    return st


def test_answer_tool_roster_report_invokes_orchestrator_mocked() -> None:
    """Full credentialing answer path: run_orchestrator mocked → markdown + extra_out populated."""
    extra_out: dict = {}
    ostate = _synthetic_ostate()
    with patch.dict("os.environ", {"CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": "http://test-skill:8011"}, clear=False):
        with patch("app.services.tool_agent._get_latest_run_for_org", return_value=None):
            with patch("app.services.tool_agent._should_run_first_of_day_reload", return_value=False):
                with patch("app.services.tool_agent.run_orchestrator", return_value=(ostate.report_final_md, ostate)):
                    ans, sources, usage, signal = answer_tool(
                        "Create a credentialing report for Synthetic Org LLC",
                        tool_hint_override="roster_report",
                        user_message="Create a credentialing report for Synthetic Org LLC",
                        extra_out=extra_out,
                    )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    assert len(ans) > 200
    assert extra_out.get("last_report_org") == "Synthetic Org LLC"
    assert extra_out.get("report_run_id") == "test-run-synthetic"
    assert isinstance(extra_out.get("roster_step_outputs"), list)
    assert len(extra_out["roster_step_outputs"]) >= 1


def test_answer_tool_roster_reconciliation_mocked_sse_complete() -> None:
    """Reconciliation path: SSE completes with final_md (httpx.Client.stream mocked)."""
    extra_out: dict = {}
    payload = {
        "event": "complete",
        "result": {
            "final_md": "## Reconciliation\n\nSynthetic reconciliation body.\n" + ("y" * 120),
            "summary": {"in_both_count": 2, "external_only_count": 1, "internal_only_count": 0},
            "report_run_id": "rec-test-1",
        },
    }
    line = "data: " + json.dumps(payload)

    class _FakeStreamResp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self):
            yield line

    stream_cm = MagicMock()
    stream_cm.__enter__.return_value = _FakeStreamResp()
    stream_cm.__exit__.return_value = None

    client_inst = MagicMock()
    client_inst.stream.return_value = stream_cm

    with patch.dict("os.environ", {"CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": "http://test-skill:8011"}, clear=False):
        with patch("app.services.tool_agent.httpx.Client") as Client:
            Client.return_value.__enter__.return_value = client_inst
            ans, sources, _, signal = answer_tool(
                "Synthetic Org LLC",
                tool_hint_override="roster_reconciliation",
                user_message="Run reconciliation",
                reconciliation_upload_id="upload-uuid-1",
                reconciliation_org_id="1234567890",
                extra_out=extra_out,
            )
    assert signal == RETRIEVAL_SIGNAL_ROSTER_COMPLETE
    assert "Roster Reconciliation Report" in ans
    assert "in_both=2" in ans
    assert extra_out.get("report_run_id") == "rec-test-1"
    assert len(sources) >= 1


def test_react_run_credentialing_report_autopilot_mocks_answer_tool() -> None:
    """ReAct executor: autopilot delegates to answer_tool (mocked) and marks success."""
    long_ans = "Credentialing result.\n\n" + ("z" * 300)
    ctx = PipelineContext(
        correlation_id="cid-test",
        thread_id="thread-test",
        message="Create a credentialing report for Acme",
        effective_message="Create a credentialing report for Acme",
    )
    with patch("app.pipeline.react_loop.answer_tool", return_value=(long_ans, [], None, RETRIEVAL_SIGNAL_ROSTER_COMPLETE)):
        out = _execute_tool(
            "run_credentialing_report",
            {"org_name": "Acme", "mode": "autopilot"},
            ctx,
            emitter=None,
        )
    assert out["tool"] == "run_credentialing_report"
    assert out["success"] is True
    assert out["result"] == long_ans
    assert ctx.active_context and ctx.active_context.get("tool") == "run_credentialing_report"


def test_react_run_credentialing_report_copilot_first_step_mocked() -> None:
    """ReAct executor: copilot creates run with no roster URL (benchmark step skipped)."""
    from app.services.credentialing_run_service import clear_runs_for_tests

    clear_runs_for_tests()
    ctx = PipelineContext(
        correlation_id="cid-cop",
        thread_id=None,
        message="Co-pilot for TinyOrg",
        effective_message="Co-pilot for TinyOrg",
    )
    try:
        with patch.dict(os.environ, {"CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": ""}, clear=False):
            out = _execute_tool(
                "run_credentialing_report",
                {"org_name": "TinyOrg", "mode": "copilot"},
                ctx,
                emitter=None,
            )
    finally:
        clear_runs_for_tests()
    assert out["tool"] == "run_credentialing_report"
    assert out["success"] is True
    res = (out.get("result") or "").lower()
    assert "co-pilot" in res or "validation" in res or "step" in res or "tinyorg" in res


@pytest.mark.credentialing_integration
@pytest.mark.skipif(
    os.environ.get("MOBIUS_RUN_CREDENTIALING_INTEGRATION", "").strip() != "1",
    reason="Set MOBIUS_RUN_CREDENTIALING_INTEGRATION=1 to run (hits real skill + LLM).",
)
def test_live_provider_roster_org_search_smoke() -> None:
    """Minimal live call: org search only (no full report). Validates URL + BQ path."""
    base = (os.environ.get("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL") or "").strip()
    if not base:
        pytest.skip("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL not set")
    import urllib.request

    url = base.rstrip("/").split("/report")[0] + "/search/org-names"
    req = urllib.request.Request(
        url,
        data=json.dumps({"name": "Synthetic Nonexistent Org XYZ123", "state": "FL", "limit": 3}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    assert "results" in data


@pytest.mark.credentialing_integration
@pytest.mark.skipif(
    os.environ.get("MOBIUS_RUN_CREDENTIALING_INTEGRATION", "").strip() != "1",
    reason="Set MOBIUS_RUN_CREDENTIALING_INTEGRATION=1",
)
def test_live_internal_skill_llm_minimal_compose() -> None:
    """One real POST /internal/skill-llm compose call (requires running mobius-chat + key)."""
    base = (os.environ.get("CREDENTIALING_LLM_ROUTER_URL") or os.environ.get("API_BASE_URL") or "").strip()
    key = (os.environ.get("MOBIUS_SKILL_LLM_INTERNAL_KEY") or "").strip()
    if not base or not key:
        pytest.skip("CREDENTIALING_LLM_ROUTER_URL and MOBIUS_SKILL_LLM_INTERNAL_KEY required")
    url = base.rstrip("/") + "/internal/skill-llm"
    r = httpx.post(
        url,
        json={
            "system": "Reply with exactly: OK",
            "user": "ping",
            "stage": "credentialing_compose",
            "max_tokens": 16,
        },
        headers={"X-Mobius-Skill-LLM-Key": key},
        timeout=120.0,
    )
    assert r.status_code == 200, r.text[:500]
    body = r.json()
    assert "text" in body
    assert (body.get("text") or "").strip()


def test_same_day_cache_serves_when_no_prefer_fresh() -> None:
    """Completed run for today → _serve_cached_credentialing_report when prefer_fresh_report is unset."""
    today_run = {
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "report_run_id": "cached-run",
        "documents": {"final_md": "## Cached report body"},
    }
    extra_out: dict = {}
    with patch.dict(os.environ, {"CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": "http://test-skill:8011"}, clear=False):
        with patch("app.services.tool_agent._get_latest_run_for_org", return_value=today_run):
            with patch("app.services.tool_agent._should_run_first_of_day_reload", return_value=False):
                with patch("app.services.tool_agent._serve_cached_credentialing_report") as mock_cached:
                    with patch("app.services.tool_agent.run_orchestrator") as mock_ro:
                        mock_cached.return_value = ("from cache", [], None, RETRIEVAL_SIGNAL_ROSTER_COMPLETE)
                        ans, _, _, _ = answer_tool(
                            "Create a credentialing report for Synthetic Org LLC",
                            tool_hint_override="roster_report",
                            user_message="Create a credentialing report for Synthetic Org LLC",
                            extra_out=extra_out,
                        )
                        mock_cached.assert_called_once()
                        mock_ro.assert_not_called()
                        assert "from cache" in ans


def test_prefer_fresh_report_bypasses_same_day_cache() -> None:
    """prefer_fresh_report=True skips same-day cache and calls run_orchestrator."""
    ostate = _synthetic_ostate()
    today_run = {
        "status": "completed",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "report_run_id": "cached-run",
        "documents": {"final_md": "## Cached"},
    }
    extra_out: dict = {}
    with patch.dict(os.environ, {"CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL": "http://test-skill:8011"}, clear=False):
        with patch("app.services.tool_agent._get_latest_run_for_org", return_value=today_run):
            with patch("app.services.tool_agent._should_run_first_of_day_reload", return_value=False):
                with patch("app.services.tool_agent._serve_cached_credentialing_report") as mock_cached:
                    with patch("app.services.tool_agent.run_orchestrator", return_value=(ostate.report_final_md, ostate)) as mock_ro:
                        answer_tool(
                            "Create a credentialing report for Synthetic Org LLC",
                            tool_hint_override="roster_report",
                            user_message="Create a credentialing report for Synthetic Org LLC",
                            extra_out=extra_out,
                            credentialing_options={"prefer_fresh_report": True},
                        )
                        mock_cached.assert_not_called()
                        mock_ro.assert_called_once()
