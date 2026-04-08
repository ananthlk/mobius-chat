"""
Integration test: task-manager signal emissions for credentialing steps 0–3.
Uses David Lawrence Center as the fixture org.

Tests verify that each step emits the correct sequence of signals to the
task manager WITHOUT requiring a live API — the provider-roster API is mocked,
and the task-manager HTTP client is intercepted so we can assert on the signal
payloads without needing the task-manager service running.

Run:
    cd mobius-chat && pytest tests/test_task_signals_steps_0_3.py -v
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.services.roster_credentialing_orchestrator import (
    OrchestratorState,
    ROSTER_CREDENTIALING_PLAN,
    StepState,
    _run_step_0_ensure_benchmarks,
    _run_step_1_identify_org,
    _run_step_2_find_locations,
    _run_step_3_find_associated_providers,
)

# ---------------------------------------------------------------------------
# DLC fixture data (mirrors real NPPES / roster-API responses)
# ---------------------------------------------------------------------------

DLC_ORG_NAME = "David Lawrence Center"
DLC_RUN_ID = "test-dlc-20260401-001"

DLC_ORG_SEARCH_RESULTS = {
    "results": [
        {"npi": "1639147086", "name": "David Lawrence Center",              "entity_type": "2", "source": "nppes", "taxonomy_code": "251S00000X"},
        {"npi": "1316944778", "name": "David Lawrence Ctr – Naples",        "entity_type": "2", "source": "nppes", "taxonomy_code": "251S00000X"},
        {"npi": "1750489023", "name": "David Lawrence Ctr – Immokalee",     "entity_type": "2", "source": "nppes", "taxonomy_code": "251S00000X"},
        {"npi": "1023041588", "name": "DLC Behavioral Health Svcs",         "entity_type": "2", "source": "nppes", "taxonomy_code": "251S00000X"},
        {"npi": "1649387210", "name": "DLC Crisis Stabilization Unit",      "entity_type": "2", "source": "nppes", "taxonomy_code": "251S00000X"},
        {"npi": "1871234509", "name": "DLC Outpatient – Marco Island",      "entity_type": "2", "source": "nppes", "taxonomy_code": "261QM0801X"},
        {"npi": "1467398021", "name": "John R. Hoffman MD",                 "entity_type": "1", "source": "nppes", "taxonomy_code": "2084P0800X"},
    ]
}

DLC_LOCATIONS = {
    "locations": [
        {"location_id": "loc-001", "npi": "1639147086", "site_address": "6075 Bathey Ln",        "site_city": "Naples",     "site_state": "FL", "site_zip": "34116", "site_source": "org_nppes"},
        {"location_id": "loc-002", "npi": "1316944778", "site_address": "4040 Tamiami Trail E",  "site_city": "Naples",     "site_state": "FL", "site_zip": "34112", "site_source": "org_nppes"},
        {"location_id": "loc-003", "npi": "1750489023", "site_address": "3370 Thomasson Dr",     "site_city": "Naples",     "site_state": "FL", "site_zip": "34109", "site_source": "org_nppes"},
        {"location_id": "loc-004", "npi": "1023041588", "site_address": "1051 Healthpark Blvd", "site_city": "Naples",     "site_state": "FL", "site_zip": "34108", "site_source": "org_nppes"},
        {"location_id": "loc-005", "npi": "1649387210", "site_address": "800 Goodlette Rd N",   "site_city": "Naples",     "site_state": "FL", "site_zip": "34102", "site_source": "org_nppes"},
        {"location_id": "loc-006", "npi": "1649387210", "site_address": "710 Goodlette Rd N",   "site_city": "Naples",     "site_state": "FL", "site_zip": "34102", "site_source": "org_nppes"},
        {"location_id": "loc-007", "npi": "1871234509", "site_address": "325 S Barfield Dr",    "site_city": "Marco Island","site_state": "FL", "site_zip": "34145", "site_source": "org_nppes"},
        {"location_id": "loc-008", "npi": "1750489023", "site_address": "321 N 9th St",         "site_city": "Immokalee",  "site_state": "FL", "site_zip": "34142", "site_source": "org_nppes"},
    ]
}

DLC_ASSOCIATED_PROVIDERS = {
    "associated_providers": {
        "loc-001": [
            {"npi": "1467907261", "name": "Abigail Pitts LCSW",   "entity_type": "1", "match_type": "nppes_address", "association_likelihood": "0.92", "roster_status": "rostered",      "inclusion_reasons": ["nppes_address_match"], "roster_rationale": "Active panel", "name_status": "ok",    "provenance": {}},
            {"npi": "1982605861", "name": "Barbara Lanz PhD",      "entity_type": "1", "match_type": "nppes_address", "association_likelihood": "0.88", "roster_status": "rostered",      "inclusion_reasons": ["nppes_address_match"], "roster_rationale": "Active panel", "name_status": "drift", "provenance": {}},
            {"npi": "1932847561", "name": "BCBS New Directions",   "entity_type": "2", "match_type": "pml_address",   "association_likelihood": "0.72", "roster_status": "external_only", "inclusion_reasons": ["pml_address"],         "roster_rationale": "",             "name_status": "ok",    "provenance": {}},
            {"npi": "1847392018", "name": "Dr. Marcus Webb MD",    "entity_type": "1", "match_type": "nppes_address", "association_likelihood": "0.81", "roster_status": "external_only", "inclusion_reasons": ["nppes_address_match"], "roster_rationale": "",             "name_status": "ok",    "provenance": {}},
        ],
        "loc-002": [
            {"npi": "1114301827", "name": "Alexis Goss PhD",       "entity_type": "1", "match_type": "nppes_address", "association_likelihood": "0.95", "roster_status": "rostered",      "inclusion_reasons": ["nppes_address_match"], "roster_rationale": "Active panel", "name_status": "ok", "provenance": {}},
            {"npi": "1902785918", "name": "Petra Camarda LCSW",    "entity_type": "1", "match_type": "nppes_address", "association_likelihood": "0.90", "roster_status": "rostered",      "inclusion_reasons": ["nppes_address_match"], "roster_rationale": "Active panel", "name_status": "ok", "provenance": {}},
            {"npi": "1723948107", "name": "Sunrise Therapy LLC",   "entity_type": "2", "match_type": "pml_address",   "association_likelihood": "0.68", "roster_status": "external_only", "inclusion_reasons": ["pml_address"],         "roster_rationale": "",             "name_status": "ok", "provenance": {}},
        ],
    },
    "active_roster": {
        "loc-001": [
            {"npi": "1467907261", "name": "Abigail Pitts LCSW"},
            {"npi": "1982605861", "name": "Barbara Lanz PhD"},
        ],
        "loc-002": [
            {"npi": "1114301827", "name": "Alexis Goss PhD"},
            {"npi": "1902785918", "name": "Petra Camarda LCSW"},
        ],
    },
    "roster_resolution": "copilot",
    "location_details": {
        "loc-001": {"location_address": "6075 Bathey Ln, Naples FL 34116"},
        "loc-002": {"location_address": "4040 Tamiami Trail E, Naples FL 34112"},
    },
    "providers_count": 7,
    "active_roster_cutoff": None,
    "compliance_candidates": [
        {"npi": "1932847561", "provider_name": "BCBS New Directions", "score": 72, "association_type": "ghost_billing"},
        {"npi": "1847392018", "provider_name": "Dr. Marcus Webb MD",  "score": 81, "association_type": "ghost_billing"},
        {"npi": "1723948107", "provider_name": "Sunrise Therapy LLC", "score": 68, "association_type": "ghost_billing"},
    ],
    "compliance_rostered_excluded": 4,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(mode: str = "copilot") -> OrchestratorState:
    s = OrchestratorState(
        org_name=DLC_ORG_NAME,
        run_id=DLC_RUN_ID,
        steps=[StepState(id=p["id"], label=p["label"]) for p in ROSTER_CREDENTIALING_PLAN],
        org_npis=[],
    )
    s.credentialing_run_mode = mode
    return s


def _fake_urlopen(response_data: dict):
    """Return a context manager that yields a fake urllib response."""
    class _Resp:
        def read(self):
            return json.dumps(response_data).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return _Resp()


def _captured_signals(mock_post) -> list[dict[str, Any]]:
    """Extract signal payloads from all calls to emit_signal."""
    signals = []
    for call in mock_post.call_args_list:
        args, kwargs = call
        signals.append(args[0] if args else kwargs)
    return signals


# ---------------------------------------------------------------------------
# Step 0 — ensure_benchmarks
# ---------------------------------------------------------------------------

class TestStep0EnsureBenchmarks:

    def test_skipped_when_no_url(self, monkeypatch):
        monkeypatch.delenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", raising=False)
        state = _make_state()
        emitted: list[tuple] = []
        with patch("app.services.roster_credentialing_orchestrator._task_signal",
                   side_effect=lambda sig, **kw: emitted.append((sig, kw))):
            _run_step_0_ensure_benchmarks(state, None)
        # State marks the step skipped
        skipped_step = next((s for s in state.steps if s.id == "ensure_benchmarks"), None)
        assert skipped_step is not None and skipped_step.status == "skipped"
        assert any(e[0] == "step_skipped" for e in emitted)

    def test_skipped_emits_step_start_then_skipped(self, monkeypatch):
        monkeypatch.delenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", raising=False)
        state = _make_state()
        emitted: list[tuple] = []

        def _capture(signal, **kwargs):
            emitted.append((signal, kwargs))

        with patch("app.services.roster_credentialing_orchestrator._task_signal",
                   side_effect=lambda sig, **kw: emitted.append((sig, kw))):
            _run_step_0_ensure_benchmarks(state, None)

        assert emitted[0][0] == "step_start"
        assert emitted[1][0] == "step_skipped"
        assert emitted[0][1]["step_id"] == "ensure_benchmarks"

    def test_done_emits_step_start_then_done(self, monkeypatch):
        monkeypatch.setenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", "http://fake:9999")
        state = _make_state()
        emitted: list[tuple] = []

        with patch("app.services.roster_credentialing_orchestrator._task_signal",
                   side_effect=lambda sig, **kw: emitted.append((sig, kw))), \
             patch("urllib.request.urlopen",
                   return_value=_fake_urlopen({"status": "ok"})):
            _run_step_0_ensure_benchmarks(state, None)

        assert emitted[0][0] == "step_start"
        assert emitted[1][0] == "step_done"

    def test_failed_emits_step_failed(self, monkeypatch):
        monkeypatch.setenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", "http://fake:9999")
        state = _make_state()
        emitted: list[tuple] = []

        with patch("app.services.roster_credentialing_orchestrator._task_signal",
                   side_effect=lambda sig, **kw: emitted.append((sig, kw))), \
             patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            _run_step_0_ensure_benchmarks(state, None)

        assert any(s[0] == "step_failed" for s in emitted)


# ---------------------------------------------------------------------------
# Step 1 — identify_org
# ---------------------------------------------------------------------------

class TestStep1IdentifyOrg:

    def _run(self, monkeypatch, mode="copilot", api_response=None, fail=False):
        monkeypatch.setenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", "http://fake:9999")
        state = _make_state(mode)
        emitted: list[tuple] = []

        urlopen_target = (
            _fake_urlopen(api_response or DLC_ORG_SEARCH_RESULTS)
            if not fail
            else None
        )
        side_effect = Exception("connection refused") if fail else None

        with patch("app.services.roster_credentialing_orchestrator._task_signal",
                   side_effect=lambda sig, **kw: emitted.append((sig, kw))), \
             patch("urllib.request.urlopen",
                   return_value=urlopen_target, side_effect=side_effect):
            _run_step_1_identify_org(DLC_ORG_NAME, state, None)

        return state, emitted

    def test_copilot_emits_step_start(self, monkeypatch):
        _, emitted = self._run(monkeypatch)
        assert emitted[0][0] == "step_start"
        assert emitted[0][1]["step_id"] == "identify_org"

    def test_copilot_emits_org_insight_card(self, monkeypatch):
        _, emitted = self._run(monkeypatch)
        insight = next((e for e in emitted if e[0] == "insight" and "NPI(s) found" in (e[1].get("title") or "")), None)
        assert insight is not None, f"No org insight card. Emitted: {[e[0] for e in emitted]}"
        assert "David Lawrence Center" in insight[1]["title"]
        assert "7" in insight[1]["title"]

    def test_copilot_emits_type1_warning_insight(self, monkeypatch):
        """The one Type 1 NPI (John R. Hoffman MD) must fire a separate insight."""
        _, emitted = self._run(monkeypatch)
        type1_insights = [e for e in emitted if e[0] == "insight" and e[1].get("issue_code") == "type1_npi_in_org_results"]
        assert len(type1_insights) == 1
        assert "1467398021" in type1_insights[0][1].get("provider_npi", "")

    def test_copilot_emits_decision_card(self, monkeypatch):
        _, emitted = self._run(monkeypatch, mode="copilot")
        decision = next((e for e in emitted if e[0] == "decision"), None)
        assert decision is not None, "No decision card emitted in copilot mode"
        assert decision[1]["step_id"] == "identify_org"

    def test_autopilot_emits_autonomous_not_decision(self, monkeypatch):
        _, emitted = self._run(monkeypatch, mode="autopilot")
        assert not any(e[0] == "decision" for e in emitted), "decision card should not fire in autopilot"
        assert any(e[0] == "autonomous" for e in emitted)

    def test_no_results_emits_blocker(self, monkeypatch):
        _, emitted = self._run(monkeypatch, api_response={"results": []})
        assert any(e[0] == "blocker" for e in emitted)
        blocker = next(e for e in emitted if e[0] == "blocker")
        assert blocker[1].get("issue_code") == "no_org_npis_found"

    def test_api_failure_emits_step_failed(self, monkeypatch):
        _, emitted = self._run(monkeypatch, fail=True)
        assert any(e[0] == "step_failed" for e in emitted)

    def test_signal_order_copilot(self, monkeypatch):
        """Expected order: step_start, insight(org), insight(type1), decision."""
        _, emitted = self._run(monkeypatch, mode="copilot")
        names = [e[0] for e in emitted]
        assert names[0] == "step_start"
        assert "insight" in names
        assert "decision" in names
        assert names.index("decision") > names.index("insight")

    def test_state_org_npis_populated(self, monkeypatch):
        state, _ = self._run(monkeypatch)
        assert len(state.org_npis) == 7  # all 7 returned (including type 1)
        assert "1639147086" in state.org_npis


# ---------------------------------------------------------------------------
# Step 2 — find_locations
# ---------------------------------------------------------------------------

class TestStep2FindLocations:

    def _run(self, monkeypatch, mode="copilot", api_response=None, fail=False):
        monkeypatch.setenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", "http://fake:9999")
        state = _make_state(mode)
        state.org_npis = ["1639147086", "1316944778", "1750489023",
                          "1023041588", "1649387210", "1871234509"]
        emitted: list[tuple] = []

        urlopen_target = (
            _fake_urlopen(api_response or DLC_LOCATIONS)
            if not fail else None
        )
        side_effect = Exception("timeout") if fail else None

        with patch("app.services.roster_credentialing_orchestrator._task_signal",
                   side_effect=lambda sig, **kw: emitted.append((sig, kw))), \
             patch("urllib.request.urlopen",
                   return_value=urlopen_target, side_effect=side_effect):
            _run_step_2_find_locations(state, None)

        return state, emitted

    def test_emits_step_start(self, monkeypatch):
        _, emitted = self._run(monkeypatch)
        assert emitted[0][0] == "step_start"

    def test_emits_locations_insight_card(self, monkeypatch):
        _, emitted = self._run(monkeypatch)
        insight = next((e for e in emitted if e[0] == "insight" and "practice site" in (e[1].get("title") or "")), None)
        assert insight is not None
        assert "8" in insight[1]["title"]
        payload = insight[1].get("data", {}).get("detail_payload", {})
        assert "rows" in payload
        assert len(payload["rows"]) == 8

    def test_copilot_emits_decision(self, monkeypatch):
        _, emitted = self._run(monkeypatch, mode="copilot")
        assert any(e[0] == "decision" for e in emitted)

    def test_autopilot_emits_autonomous(self, monkeypatch):
        _, emitted = self._run(monkeypatch, mode="autopilot")
        assert any(e[0] == "autonomous" for e in emitted)
        assert not any(e[0] == "decision" for e in emitted)

    def test_skipped_when_no_npis(self, monkeypatch):
        monkeypatch.setenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", "http://fake:9999")
        state = _make_state()
        state.org_npis = []
        emitted: list[tuple] = []
        with patch("app.services.roster_credentialing_orchestrator._task_signal",
                   side_effect=lambda sig, **kw: emitted.append((sig, kw))):
            _run_step_2_find_locations(state, None)
        assert any(e[0] == "step_skipped" for e in emitted)

    def test_api_error_emits_paused(self, monkeypatch):
        _, emitted = self._run(monkeypatch, fail=True)
        assert any(e[0] == "paused" for e in emitted)

    def test_state_locations_populated(self, monkeypatch):
        state, _ = self._run(monkeypatch)
        assert len(state.locations) == 8


# ---------------------------------------------------------------------------
# Step 3 — find_associated_providers
# ---------------------------------------------------------------------------

class TestStep3FindAssociatedProviders:

    def _run(self, monkeypatch, mode="copilot", api_response=None, fail=False):
        monkeypatch.setenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", "http://fake:9999")
        state = _make_state(mode)
        state.org_npis = ["1639147086", "1316944778"]
        state.locations = DLC_LOCATIONS["locations"][:2]
        emitted: list[tuple] = []

        urlopen_target = (
            _fake_urlopen(api_response or DLC_ASSOCIATED_PROVIDERS)
            if not fail else None
        )
        side_effect = Exception("timeout") if fail else None

        with patch("app.services.roster_credentialing_orchestrator._task_signal",
                   side_effect=lambda sig, **kw: emitted.append((sig, kw))), \
             patch("urllib.request.urlopen",
                   return_value=urlopen_target, side_effect=side_effect):
            _run_step_3_find_associated_providers(state, None)

        return state, emitted

    def test_emits_step_start(self, monkeypatch):
        _, emitted = self._run(monkeypatch)
        assert emitted[0][0] == "step_start"

    def test_emits_provider_pool_insight(self, monkeypatch):
        _, emitted = self._run(monkeypatch)
        pool = next((e for e in emitted if e[0] == "insight" and "provider(s) found" in (e[1].get("title") or "")), None)
        assert pool is not None
        assert "7" in pool[1]["title"]

    def test_emits_ghost_billing_insight_when_candidates_present(self, monkeypatch):
        _, emitted = self._run(monkeypatch)
        ghost = next((e for e in emitted if e[0] == "insight" and e[1].get("issue_code") == "ghost_billing_candidates"), None)
        assert ghost is not None, "Expected ghost billing insight card"
        assert "3" in ghost[1]["title"]

    def test_no_ghost_billing_insight_when_no_candidates(self, monkeypatch):
        response = {**DLC_ASSOCIATED_PROVIDERS, "compliance_candidates": []}
        _, emitted = self._run(monkeypatch, api_response=response)
        assert not any(e[1].get("issue_code") == "ghost_billing_candidates" for e in emitted)

    def test_copilot_emits_decision(self, monkeypatch):
        _, emitted = self._run(monkeypatch, mode="copilot")
        assert any(e[0] == "decision" for e in emitted)

    def test_autopilot_emits_autonomous(self, monkeypatch):
        _, emitted = self._run(monkeypatch, mode="autopilot")
        assert any(e[0] == "autonomous" for e in emitted)
        assert not any(e[0] == "decision" for e in emitted)

    def test_ghost_billing_insight_has_candidate_rows(self, monkeypatch):
        _, emitted = self._run(monkeypatch)
        ghost = next(e for e in emitted if e[1].get("issue_code") == "ghost_billing_candidates")
        rows = ghost[1]["data"]["detail_payload"]["rows"]
        assert len(rows) == 3
        npis = [r["npi"] for r in rows]
        assert "1932847561" in npis  # BCBS New Directions
        assert "1847392018" in npis  # Dr. Marcus Webb

    def test_skipped_when_no_locations(self, monkeypatch):
        monkeypatch.setenv("CHAT_SKILLS_PROVIDER_ROSTER_CREDENTIALING_URL", "http://fake:9999")
        state = _make_state()
        state.org_npis = ["1639147086"]
        state.locations = []
        emitted: list[tuple] = []
        with patch("app.services.roster_credentialing_orchestrator._task_signal",
                   side_effect=lambda sig, **kw: emitted.append((sig, kw))):
            _run_step_3_find_associated_providers(state, None)
        assert any(e[0] == "step_skipped" for e in emitted)

    def test_state_compliance_candidates_populated(self, monkeypatch):
        state, _ = self._run(monkeypatch)
        assert len(state.compliance_candidates) == 3

    def test_signal_order(self, monkeypatch):
        """step_start → insight(pool) → insight(ghost billing) → decision."""
        _, emitted = self._run(monkeypatch, mode="copilot")
        names = [e[0] for e in emitted]
        assert names[0] == "step_start"
        pool_idx  = next(i for i, e in enumerate(emitted) if e[0] == "insight" and "provider(s) found" in (e[1].get("title") or ""))
        ghost_idx = next(i for i, e in enumerate(emitted) if e[1].get("issue_code") == "ghost_billing_candidates")
        dec_idx   = next(i for i, e in enumerate(emitted) if e[0] == "decision")
        assert pool_idx < ghost_idx < dec_idx
