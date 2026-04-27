"""Tests for app/services/model_profile.py + admin profile endpoints.

Coverage:
  * YAML loader (missing file, malformed file, default-only graceful fallback)
  * Active-profile resolution priority (override > env > default)
  * Per-stage pin lookup
  * Pinned-model-missing → fallback_model path
  * PHI safety: pinned non-HIPAA model skipped when phi_detected=True
  * Admin endpoint gating via MOBIUS_ADMIN_ENABLED
  * Admin GET + POST round-trip + invalid-profile 400
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import model_profile as mp


@pytest.fixture(autouse=True)
def _reset_profile_state():
    """Every test starts with a fresh loader + no runtime override."""
    mp._reset_for_tests()
    yield
    mp._reset_for_tests()


def _write_profile_yaml(tmp_path: Path, body: str) -> Path:
    """Helper — write a YAML file and point the loader at it."""
    p = tmp_path / "profiles.yaml"
    p.write_text(textwrap.dedent(body))
    os.environ["MOBIUS_MODEL_PROFILE_FILE"] = str(p)
    return p


# ── Loader ────────────────────────────────────────────────────────────


class TestLoader:
    def test_missing_file_returns_canonical_empty_profiles(self, monkeypatch):
        """2026-04-27: ``auto`` is now the canonical empty-map profile;
        ``default`` and ``bandit`` are kept as deprecated aliases for
        env-var / admin-API stability. All three must be present so a
        broken / missing config still surfaces a usable picker."""
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE_FILE", "/nonexistent.yaml")
        profiles = mp._load()
        assert profiles == {"auto": {}, "default": {}, "bandit": {}}

    def test_malformed_yaml_falls_back_to_canonical_profiles(self, tmp_path, monkeypatch):
        p = tmp_path / "bad.yaml"
        p.write_text("not: valid: yaml: at: all: [")
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE_FILE", str(p))
        profiles = mp._load()
        assert profiles == {"auto": {}, "default": {}, "bandit": {}}

    def test_well_formed_yaml_loads(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo:
                integrator: claude-3-5-sonnet-20241022
                fallback_model: gemini-2.5-flash
        """)
        profiles = mp._load()
        assert "demo" in profiles
        assert profiles["demo"]["integrator"] == "claude-3-5-sonnet-20241022"

    def test_missing_default_key_is_auto_added(self, tmp_path, monkeypatch):
        """Even if the YAML omits ``default:``, we always expose it as
        an empty profile so callers get predictable ``default`` → no-op
        semantics."""
        _write_profile_yaml(tmp_path, """
            profiles:
              demo:
                integrator: gemini-2.5-flash
        """)
        profiles = mp._load()
        assert "default" in profiles
        assert profiles["default"] == {}


# ── Active-profile resolution priority ────────────────────────────────


class TestActiveProfileResolution:
    def test_default_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("MOBIUS_MODEL_PROFILE", raising=False)
        assert mp.get_active_profile_name() == "default"

    def test_env_var_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE", "demo")
        assert mp.get_active_profile_name() == "demo"

    def test_runtime_override_wins_over_env(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo: {}
              anthropic_first: {}
        """)
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE", "demo")
        mp.set_active_profile("anthropic_first")
        assert mp.get_active_profile_name() == "anthropic_first"

    def test_clearing_override_reverts_to_env(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo: {}
        """)
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE", "demo")
        mp.set_active_profile("default")
        assert mp.get_active_profile_name() == "default"
        mp.set_active_profile(None)
        assert mp.get_active_profile_name() == "demo"

    def test_unknown_profile_raises(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, "profiles: {default: {}}")
        with pytest.raises(ValueError) as excinfo:
            mp.set_active_profile("does-not-exist")
        assert "does-not-exist" in str(excinfo.value)


# ── Per-stage pin lookup ──────────────────────────────────────────────


class TestPinLookup:
    def test_pinned_model_for_stage_reads_profile(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo:
                react_1: gemini-2.5-flash
                integrator: claude-3-5-sonnet-20241022
        """)
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE", "demo")
        assert mp.pinned_model_for_stage("react_1") == "gemini-2.5-flash"
        assert mp.pinned_model_for_stage("integrator") == "claude-3-5-sonnet-20241022"

    def test_unpinned_stage_returns_none(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo:
                react_1: gemini-2.5-flash
        """)
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE", "demo")
        assert mp.pinned_model_for_stage("critique") is None

    def test_default_profile_pins_nothing(self, monkeypatch):
        monkeypatch.delenv("MOBIUS_MODEL_PROFILE", raising=False)
        assert mp.pinned_model_for_stage("react_1") is None

    def test_exclude_providers_parsed(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              no_groq:
                exclude_providers: [groq, foobar]
        """)
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE", "no_groq")
        assert mp.excluded_providers() == frozenset({"groq", "foobar"})


# ── resolve_pinned_model (integration with MODEL_ROSTER) ─────────────


class TestResolvePinnedModel:
    def test_returns_none_when_no_pin(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, "profiles: {default: {}}")
        monkeypatch.delenv("MOBIUS_MODEL_PROFILE", raising=False)
        spec, meta = mp.resolve_pinned_model("react_1")
        assert spec is None
        assert meta["model_profile"] == "default"

    def test_returns_spec_when_pin_exists_in_roster(self, tmp_path, monkeypatch):
        """gemini-2.5-flash is a known ROSTER entry in MODEL_ROSTER."""
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo:
                react_1: gemini-2.5-flash
        """)
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE", "demo")
        spec, meta = mp.resolve_pinned_model("react_1")
        assert spec is not None
        assert spec.model_id == "gemini-2.5-flash"
        assert meta["profile_pin"] is True
        assert meta["profile_pinned_model"] == "gemini-2.5-flash"

    def test_falls_back_to_fallback_model_when_pin_missing(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo:
                react_1: made-up-model-xyz
                fallback_model: gemini-2.5-flash
        """)
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE", "demo")
        spec, meta = mp.resolve_pinned_model("react_1")
        assert spec is not None
        assert spec.model_id == "gemini-2.5-flash"
        assert meta["profile_fallback_used"] == "gemini-2.5-flash"
        assert meta["profile_pin_attempted"] == "made-up-model-xyz"

    def test_returns_none_when_both_pin_and_fallback_missing(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo:
                react_1: made-up-1
                fallback_model: made-up-2
        """)
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE", "demo")
        spec, meta = mp.resolve_pinned_model("react_1")
        assert spec is None
        assert meta["profile_pin_missing"] == "made-up-1"

    def test_phi_skips_non_hipaa_pin(self, tmp_path, monkeypatch):
        """PHI-detected turns must NOT use a pinned non-HIPAA model.
        gemini-2.5-flash happens to have hipaa_eligible=True in the
        roster; we need a known non-HIPAA entry to test the skip
        path. Monkey-patch the spec's attribute."""
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo:
                react_1: gemini-2.5-flash
        """)
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE", "demo")
        from app.services.model_registry import MODEL_ROSTER
        real_spec = MODEL_ROSTER["gemini-2.5-flash"]
        with patch.object(type(real_spec), "hipaa_eligible", new=False, create=True):
            # object-level patch won't work on frozen dataclass; use a
            # plain Mock wrapper if needed. Simpler: construct a
            # stand-in with hipaa_eligible=False and patch MODEL_ROSTER.
            pass

        # Patch the roster entry to a stand-in with hipaa_eligible=False.
        class _Stand:
            model_id = "gemini-2.5-flash"
            hipaa_eligible = False
            provider = "vertex"

        with patch.dict(MODEL_ROSTER, {"gemini-2.5-flash": _Stand()}):
            spec, meta = mp.resolve_pinned_model("react_1", phi_detected=True)
            assert spec is None
            assert meta.get("profile_phi_skip_pin") == "gemini-2.5-flash"


# ── Admin endpoints ───────────────────────────────────────────────────


class TestAdminEndpoints:
    @staticmethod
    def _build_app() -> FastAPI:
        from app.api.admin import router
        app = FastAPI()
        app.include_router(router)
        return app

    def test_get_returns_404_when_admin_disabled(self, monkeypatch):
        monkeypatch.delenv("MOBIUS_ADMIN_ENABLED", raising=False)
        monkeypatch.delenv("MOBIUS_DEV_TOKEN_ENABLED", raising=False)
        client = TestClient(self._build_app())
        r = client.get("/chat/admin/model-profile")
        assert r.status_code == 404

    def test_get_returns_state_when_enabled(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo: {integrator: gemini-2.5-flash}
        """)
        monkeypatch.setenv("MOBIUS_ADMIN_ENABLED", "1")
        monkeypatch.delenv("MOBIUS_MODEL_PROFILE", raising=False)
        client = TestClient(self._build_app())
        r = client.get("/chat/admin/model-profile")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["active_profile"] == "default"
        assert "demo" in body["available_profiles"]
        assert body["override_set"] is False

    def test_post_switches_profile(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo: {integrator: gemini-2.5-flash}
        """)
        monkeypatch.setenv("MOBIUS_ADMIN_ENABLED", "1")
        monkeypatch.delenv("MOBIUS_MODEL_PROFILE", raising=False)
        client = TestClient(self._build_app())
        r = client.post("/chat/admin/model-profile", json={"profile": "demo"})
        assert r.status_code == 200
        body = r.json()
        assert body["active_profile"] == "demo"
        assert body["override_set"] is True

        # Follow-up GET reflects the new state.
        r2 = client.get("/chat/admin/model-profile")
        assert r2.json()["active_profile"] == "demo"

    def test_post_null_clears_override(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, """
            profiles:
              default: {}
              demo: {}
        """)
        monkeypatch.setenv("MOBIUS_ADMIN_ENABLED", "1")
        monkeypatch.setenv("MOBIUS_MODEL_PROFILE", "demo")
        client = TestClient(self._build_app())
        # First, set an override.
        client.post("/chat/admin/model-profile", json={"profile": "default"})
        # Now clear it → revert to env (demo).
        r = client.post("/chat/admin/model-profile", json={"profile": None})
        assert r.status_code == 200
        assert r.json()["active_profile"] == "demo"
        assert r.json()["override_set"] is False

    def test_post_unknown_profile_400(self, tmp_path, monkeypatch):
        _write_profile_yaml(tmp_path, "profiles: {default: {}}")
        monkeypatch.setenv("MOBIUS_ADMIN_ENABLED", "1")
        client = TestClient(self._build_app())
        r = client.post("/chat/admin/model-profile", json={"profile": "does-not-exist"})
        assert r.status_code == 400
        assert "does-not-exist" in r.json()["detail"]

    def test_admin_flag_overrides_dev_token_flag(self, tmp_path, monkeypatch):
        """MOBIUS_ADMIN_ENABLED=1 works even when MOBIUS_DEV_TOKEN_ENABLED
        is unset — ops can enable the model-profile surface without
        exposing token minting."""
        _write_profile_yaml(tmp_path, "profiles: {default: {}}")
        monkeypatch.setenv("MOBIUS_ADMIN_ENABLED", "1")
        monkeypatch.delenv("MOBIUS_DEV_TOKEN_ENABLED", raising=False)
        client = TestClient(self._build_app())
        r = client.get("/chat/admin/model-profile")
        assert r.status_code == 200
