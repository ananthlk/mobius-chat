"""Tests for the dev-only token minter (2026-04-23).

The endpoint exists so bench harnesses and local frontends can exercise
the authed path without a running mobius-os. Production safety hinges
on the env gate: when ``MOBIUS_DEV_TOKEN_ENABLED`` is absent, the
endpoint returns 404 (NOT 403 — a 404 leaves no surface for
attackers to fingerprint).

Also verifies the minted token actually validates through chat's own
``get_user_id_from_token`` path — if it didn't, L3 rate limiting would
silently not activate.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app_with_admin_router(secret_value: str | None = "test-secret-12345") -> FastAPI:
    from app.api.admin import router
    app = FastAPI()
    app.include_router(router)
    # Patch the secret lookup for the entire app lifetime.
    patcher = patch("app.api.admin.get_secret",
                    return_value=secret_value)
    patcher.start()
    return app


class TestDevTokenGate:
    def test_returns_404_when_env_unset(self, monkeypatch):
        """Master kill switch: endpoint must look non-existent when
        MOBIUS_DEV_TOKEN_ENABLED is not explicitly enabled."""
        monkeypatch.delenv("MOBIUS_DEV_TOKEN_ENABLED", raising=False)
        app = _app_with_admin_router()
        client = TestClient(app)
        r = client.post("/chat/admin/mint-dev-token", json={})
        assert r.status_code == 404

    def test_returns_404_when_env_disabled(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_DEV_TOKEN_ENABLED", "0")
        app = _app_with_admin_router()
        client = TestClient(app)
        r = client.post("/chat/admin/mint-dev-token", json={})
        assert r.status_code == 404

    def test_returns_404_when_env_false(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_DEV_TOKEN_ENABLED", "false")
        app = _app_with_admin_router()
        client = TestClient(app)
        r = client.post("/chat/admin/mint-dev-token", json={})
        assert r.status_code == 404

    def test_mints_when_enabled(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_DEV_TOKEN_ENABLED", "1")
        app = _app_with_admin_router()
        client = TestClient(app)
        r = client.post("/chat/admin/mint-dev-token", json={})
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert body["access_token"].count(".") == 2   # JWT = header.payload.sig
        assert body["user_id"]
        assert body["tenant_id"]
        assert body["ttl_seconds"] > 0
        assert "expires_at" in body
        assert "warning" in body
        assert "production" in body["warning"].lower()


class TestDevTokenCustomization:
    def test_custom_user_id_preserved(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_DEV_TOKEN_ENABLED", "1")
        app = _app_with_admin_router()
        client = TestClient(app)
        r = client.post("/chat/admin/mint-dev-token",
                        json={"user_id": "alice-test"})
        assert r.status_code == 200
        assert r.json()["user_id"] == "alice-test"

    def test_custom_tenant_id_preserved(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_DEV_TOKEN_ENABLED", "1")
        app = _app_with_admin_router()
        client = TestClient(app)
        r = client.post("/chat/admin/mint-dev-token",
                        json={"tenant_id": "tenant-xyz"})
        assert r.status_code == 200
        assert r.json()["tenant_id"] == "tenant-xyz"

    def test_ttl_clamped_to_max_1_day(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_DEV_TOKEN_ENABLED", "1")
        app = _app_with_admin_router()
        client = TestClient(app)
        r = client.post("/chat/admin/mint-dev-token",
                        json={"ttl_seconds": 999999})  # way over 1 day
        assert r.status_code == 200
        assert r.json()["ttl_seconds"] == 86400

    def test_ttl_clamped_to_min_60s(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_DEV_TOKEN_ENABLED", "1")
        app = _app_with_admin_router()
        client = TestClient(app)
        r = client.post("/chat/admin/mint-dev-token",
                        json={"ttl_seconds": 1})  # way under 60s
        assert r.status_code == 200
        assert r.json()["ttl_seconds"] == 60


class TestDevTokenErrorPaths:
    def test_500_when_secret_missing(self, monkeypatch):
        """JWT_SECRET absent → fail loud. Silent mint-without-validation
        would be actively misleading (tokens wouldn't decode)."""
        monkeypatch.setenv("MOBIUS_DEV_TOKEN_ENABLED", "1")
        app = _app_with_admin_router(secret_value=None)
        client = TestClient(app)
        r = client.post("/chat/admin/mint-dev-token", json={})
        assert r.status_code == 500
        assert "JWT_SECRET" in r.json()["detail"]


class TestDevTokenRoundTripValidation:
    """End-to-end: token minted by admin router should decode via
    chat's own ``get_user_id_from_token``. If this breaks, L3 rate
    limiting silently won't activate in prod — worth a regression
    guard."""

    def test_minted_token_decodes_via_chat_validator(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_DEV_TOKEN_ENABLED", "1")
        monkeypatch.setenv("MOBIUS_OS_AUTH_URL", "https://placeholder.invalid")
        # Both sides must see the same secret for the round-trip to work.
        fake_secret = "shared-secret-abc"
        app = _app_with_admin_router(secret_value=fake_secret)
        client = TestClient(app)
        r = client.post("/chat/admin/mint-dev-token",
                        json={"user_id": "alice-rt"})
        token = r.json()["access_token"]

        # Chat's validator reads the secret through the same helper —
        # patch both sides to return the same value.
        with patch("app.auth.get_secret", return_value=fake_secret):
            from app.auth import get_user_id_from_token
            assert get_user_id_from_token(token) == "alice-rt"

    def test_tampered_token_rejected(self, monkeypatch):
        monkeypatch.setenv("MOBIUS_DEV_TOKEN_ENABLED", "1")
        monkeypatch.setenv("MOBIUS_OS_AUTH_URL", "https://placeholder.invalid")
        fake_secret = "shared-secret-abc"
        app = _app_with_admin_router(secret_value=fake_secret)
        client = TestClient(app)
        r = client.post("/chat/admin/mint-dev-token", json={})
        good_token = r.json()["access_token"]
        # Flip a character in the signature — must fail validation.
        tampered = good_token[:-5] + ("B" if good_token[-5] == "A" else "A") + good_token[-4:]

        with patch("app.auth.get_secret", return_value=fake_secret):
            from app.auth import get_user_id_from_token
            assert get_user_id_from_token(tampered) is None
