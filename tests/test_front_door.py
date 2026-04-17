"""Phase 1h — front-door hardening tests.

Three surfaces are covered here:

1. CORS: permissive default in dev; fail-closed in staging/prod unless
   ``CHAT_CORS_ORIGINS`` is set; wildcards rejected in hosted envs.

2. Rate limit: opt-in via ``CHAT_RATE_LIMIT_PER_MINUTE``; default-on in
   hosted envs; sliding-window per-IP; 429 with Retry-After header when
   the bucket overflows.

3. Auth mode: ``off`` / ``optional`` / ``required``. ``require_user``
   dependency returns None in off, decodes-but-doesn't-reject in
   optional, and 401s in required when no valid JWT.

The audit flagged main.py:242 `allow_origins=["*"]` and the fact that
`app/auth.py` was imported by nothing as the two highest-risk front-door
gaps. These tests lock the fix in.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.front_door import (
    CorsConfig,
    CorsMisconfiguredError,
    InMemoryRateLimitMiddleware,
    RateLimitConfig,
    auth_mode,
    chat_env,
    is_hosted,
    require_user,
    resolve_cors_config,
    resolve_rate_limit_config,
)


# ── Environment gate ──────────────────────────────────────────────────────


class TestChatEnvGate:
    def test_default_is_dev(self, monkeypatch):
        monkeypatch.delenv("CHAT_ENV", raising=False)
        assert chat_env() == "dev"
        assert is_hosted() is False

    @pytest.mark.parametrize("val", ["staging", "STAGING", "  staging  "])
    def test_staging_normalized(self, monkeypatch, val):
        monkeypatch.setenv("CHAT_ENV", val)
        assert chat_env() == "staging"
        assert is_hosted() is True

    def test_prod_is_hosted(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        assert is_hosted() is True

    def test_unknown_value_falls_back_to_dev(self, monkeypatch):
        """A typo'd CHAT_ENV must NOT silently fail closed (would take down
        the whole app) — fall back to dev and log a warning."""
        monkeypatch.setenv("CHAT_ENV", "production")  # common typo
        assert chat_env() == "dev"


# ── CORS ──────────────────────────────────────────────────────────────────


class TestCorsConfig:
    def test_dev_default_is_wildcard(self, monkeypatch):
        monkeypatch.delenv("CHAT_ENV", raising=False)
        monkeypatch.delenv("CHAT_CORS_ORIGINS", raising=False)
        cfg = resolve_cors_config()
        assert cfg.allow_origins == ["*"]
        # Starlette ignores allow_credentials=True with wildcard — keep aligned.
        assert cfg.allow_credentials is False

    def test_dev_can_override_with_explicit_list(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "dev")
        monkeypatch.setenv(
            "CHAT_CORS_ORIGINS",
            "http://localhost:3000, http://localhost:5173 ",
        )
        cfg = resolve_cors_config()
        assert cfg.allow_origins == ["http://localhost:3000", "http://localhost:5173"]
        assert cfg.allow_credentials is True

    def test_hosted_without_origins_raises(self, monkeypatch):
        """The whole point of 1h: CHAT_ENV=prod + no origins must FAIL at
        startup, not silently ship with ['*']."""
        monkeypatch.setenv("CHAT_ENV", "prod")
        monkeypatch.delenv("CHAT_CORS_ORIGINS", raising=False)
        with pytest.raises(CorsMisconfiguredError) as excinfo:
            resolve_cors_config()
        assert "CHAT_CORS_ORIGINS" in str(excinfo.value)

    def test_hosted_rejects_wildcard_origins(self, monkeypatch):
        """Wildcards in hosted env are a silent hole — subdomain matching
        doesn't do what operators think it does. Reject at config time."""
        monkeypatch.setenv("CHAT_ENV", "staging")
        monkeypatch.setenv("CHAT_CORS_ORIGINS", "https://*.example.com")
        with pytest.raises(CorsMisconfiguredError) as excinfo:
            resolve_cors_config()
        assert "wildcard" in str(excinfo.value).lower()

    def test_hosted_restricts_methods_and_headers(self, monkeypatch):
        """Hosted env should NOT use allow_methods=['*'] — only the verbs
        chat actually uses."""
        monkeypatch.setenv("CHAT_ENV", "prod")
        monkeypatch.setenv("CHAT_CORS_ORIGINS", "https://app.example.com")
        cfg = resolve_cors_config()
        assert "*" not in cfg.allow_methods
        assert "*" not in cfg.allow_headers
        assert "POST" in cfg.allow_methods
        assert "Authorization" in cfg.allow_headers

    def test_trailing_slashes_stripped(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        monkeypatch.setenv("CHAT_CORS_ORIGINS", "https://a.example.com/,https://b.example.com/")
        cfg = resolve_cors_config()
        assert cfg.allow_origins == ["https://a.example.com", "https://b.example.com"]


# ── Rate limit config ─────────────────────────────────────────────────────


class TestRateLimitConfig:
    def test_dev_default_off(self, monkeypatch):
        monkeypatch.delenv("CHAT_ENV", raising=False)
        monkeypatch.delenv("CHAT_RATE_LIMIT_PER_MINUTE", raising=False)
        cfg = resolve_rate_limit_config()
        assert cfg.enabled is False

    def test_hosted_default_30_rpm_on_chat(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        monkeypatch.delenv("CHAT_RATE_LIMIT_PER_MINUTE", raising=False)
        cfg = resolve_rate_limit_config()
        assert cfg.enabled is True
        assert cfg.requests_per_minute == 30
        assert "/chat" in cfg.path_prefixes

    def test_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "dev")  # would be off by default
        monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "5")
        cfg = resolve_rate_limit_config()
        assert cfg.enabled is True
        assert cfg.requests_per_minute == 5

    def test_invalid_override_disables(self, monkeypatch):
        """Garbage in env should disable rate limiting, not crash startup."""
        monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "not-a-number")
        cfg = resolve_rate_limit_config()
        assert cfg.enabled is False


# ── Rate limit middleware behavior ────────────────────────────────────────


def _app_with_rate_limit(rpm: int, paths: tuple[str, ...] = ("/chat",)) -> FastAPI:
    """Tiny test-only app with the limiter mounted and two endpoints."""
    app = FastAPI()
    app.add_middleware(
        InMemoryRateLimitMiddleware,
        config=RateLimitConfig(enabled=True, requests_per_minute=rpm, path_prefixes=paths),
    )

    @app.get("/chat/ping")
    def ping():
        return {"ok": True}

    @app.get("/unlimited")
    def unlimited():
        return {"ok": True}

    return app


class TestRateLimitMiddleware:
    def test_allows_requests_under_limit(self):
        app = _app_with_rate_limit(rpm=5)
        client = TestClient(app)
        for _ in range(5):
            assert client.get("/chat/ping").status_code == 200

    def test_429_once_limit_exceeded(self):
        app = _app_with_rate_limit(rpm=3)
        client = TestClient(app)
        for _ in range(3):
            assert client.get("/chat/ping").status_code == 200
        resp = client.get("/chat/ping")
        assert resp.status_code == 429
        body = resp.json()
        assert "retry_after_seconds" in body
        assert "Retry-After" in resp.headers

    def test_untracked_path_not_limited(self):
        """Paths outside path_prefixes are bypassed."""
        app = _app_with_rate_limit(rpm=2, paths=("/chat",))
        client = TestClient(app)
        # Exhaust the limit for /chat.
        for _ in range(2):
            client.get("/chat/ping")
        # /unlimited must still succeed.
        assert client.get("/unlimited").status_code == 200

    def test_disabled_config_bypasses_entirely(self):
        app = FastAPI()
        app.add_middleware(
            InMemoryRateLimitMiddleware,
            config=RateLimitConfig(enabled=False, requests_per_minute=0, path_prefixes=()),
        )

        @app.get("/chat/ping")
        def ping():
            return {"ok": True}

        client = TestClient(app)
        # Way more than any sane limit — must all succeed.
        for _ in range(50):
            assert client.get("/chat/ping").status_code == 200

    def test_sliding_window_reclaims_slots(self):
        """After the 60s window elapses, old timestamps age out. Simulate
        by reaching into the middleware's bucket directly — stable and
        deterministic."""
        app = _app_with_rate_limit(rpm=2)
        mw = next(
            m for m in app.user_middleware if m.cls is InMemoryRateLimitMiddleware
        )
        client = TestClient(app)
        # Fill the bucket.
        assert client.get("/chat/ping").status_code == 200
        assert client.get("/chat/ping").status_code == 200
        assert client.get("/chat/ping").status_code == 429

        # Age out: walk the bucket dict and push timestamps >60s into the past.
        # Need to access the actual middleware instance, which FastAPI wraps
        # — easier to just patch time.monotonic.
        with patch("app.api.front_door.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 120
            assert client.get("/chat/ping").status_code == 200


# ── Auth mode ─────────────────────────────────────────────────────────────


class TestAuthMode:
    def test_dev_default_off(self, monkeypatch):
        monkeypatch.delenv("CHAT_ENV", raising=False)
        monkeypatch.delenv("CHAT_AUTH_MODE", raising=False)
        assert auth_mode() == "off"

    def test_hosted_default_required(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        monkeypatch.delenv("CHAT_AUTH_MODE", raising=False)
        assert auth_mode() == "required"

    def test_explicit_override(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "dev")
        monkeypatch.setenv("CHAT_AUTH_MODE", "required")
        assert auth_mode() == "required"

    def test_unknown_value_falls_through_to_env_default(self, monkeypatch):
        """Typo in CHAT_AUTH_MODE mustn't disable auth in hosted env."""
        monkeypatch.setenv("CHAT_ENV", "prod")
        monkeypatch.setenv("CHAT_AUTH_MODE", "not-a-mode")
        assert auth_mode() == "required"


def _app_with_require_user() -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    def protected(user_id: str | None = Depends(require_user)):
        return {"user_id": user_id}

    return app


class TestRequireUserDependency:
    def test_off_mode_passes_through_as_none(self, monkeypatch):
        monkeypatch.setenv("CHAT_AUTH_MODE", "off")
        client = TestClient(_app_with_require_user())
        resp = client.get("/protected")
        assert resp.status_code == 200
        assert resp.json() == {"user_id": None}

    def test_required_mode_401_without_token(self, monkeypatch):
        monkeypatch.setenv("CHAT_AUTH_MODE", "required")
        client = TestClient(_app_with_require_user())
        resp = client.get("/protected")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_required_mode_401_with_invalid_token(self, monkeypatch):
        monkeypatch.setenv("CHAT_AUTH_MODE", "required")
        client = TestClient(_app_with_require_user())
        resp = client.get("/protected", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401

    def test_required_mode_passes_with_valid_token(self, monkeypatch):
        monkeypatch.setenv("CHAT_AUTH_MODE", "required")
        client = TestClient(_app_with_require_user())
        # Stub the token decoder — we're testing the dependency, not JWT.
        with patch("app.auth.get_user_id_from_request", return_value="u-42"):
            resp = client.get("/protected", headers={"Authorization": "Bearer good"})
        assert resp.status_code == 200
        assert resp.json() == {"user_id": "u-42"}

    def test_optional_mode_allows_no_token(self, monkeypatch):
        """Optional: decode when present, allow when absent. Useful for
        mixed routers where some endpoints benefit from the user_id but
        none require it."""
        monkeypatch.setenv("CHAT_AUTH_MODE", "optional")
        client = TestClient(_app_with_require_user())
        resp = client.get("/protected")
        assert resp.status_code == 200
        assert resp.json() == {"user_id": None}

    def test_optional_mode_decodes_when_present(self, monkeypatch):
        monkeypatch.setenv("CHAT_AUTH_MODE", "optional")
        client = TestClient(_app_with_require_user())
        with patch("app.auth.get_user_id_from_request", return_value="u-1"):
            resp = client.get("/protected", headers={"Authorization": "Bearer ok"})
        assert resp.json() == {"user_id": "u-1"}


# ── Regression against the exact gap the audit flagged ────────────────────


class TestAuditRegression:
    """The comprehensive audit (2026-04-17) flagged:

        main.py:242 -> allow_origins=["*"]
        app/auth.py imported by nothing

    Lock both in place as regression tests so they can't silently revert.
    """

    def test_main_py_no_longer_inlines_wildcard_cors(self):
        from pathlib import Path
        main_py = Path(__file__).parent.parent / "app" / "main.py"
        text = main_py.read_text()
        # Look for the exact anti-pattern the audit called out.
        bad = 'allow_origins=["*"]'
        assert bad not in text, (
            f"main.py still contains {bad!r}. Phase 1h moved CORS config "
            f"to app.api.front_door.resolve_cors_config; re-inlining the "
            f"wildcard re-introduces the audit finding."
        )

    def test_front_door_is_imported_by_main(self):
        """If main.py stops importing from front_door, the hardening is
        disabled even though the module still exists."""
        from pathlib import Path
        main_py = Path(__file__).parent.parent / "app" / "main.py"
        text = main_py.read_text()
        assert "from app.api.front_door import" in text, (
            "main.py no longer imports front_door — CORS + rate-limit "
            "config won't be applied."
        )

    def test_tasks_router_uses_require_user(self):
        """Phase 1h wired require_user onto tasks router write paths as
        proof-of-pattern. If it's been removed, the auth-mode switch has
        no effect on that router."""
        from pathlib import Path
        tasks_py = Path(__file__).parent.parent / "app" / "api" / "tasks.py"
        text = tasks_py.read_text()
        assert "from app.api.front_door import require_user" in text
        # Should appear on at least the 4 write routes.
        assert text.count("Depends(require_user)") >= 4, (
            "Fewer than 4 write routes in tasks.py use Depends(require_user). "
            "Phase 1h wired it to POST /chat/tasks, POST /chat/tasks/bulk-import, "
            "PATCH /chat/tasks/{id}, POST .../resolve, POST .../dismiss."
        )
