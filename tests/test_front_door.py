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


# ── Tiered rate limiter: L1 / L2 / L3 + exemptions (2026-04-23) ─────


def _tiered_app(
    *,
    l1_rpm: int = 100,
    l2_rpm: int = 0,
    l3_rpm: int = 0,
    exempt_ips: frozenset[str] = frozenset(),
) -> FastAPI:
    """Tiny app for the multi-tier tests. POST /chat accepts a JSON body
    with ``thread_id`` (for L2 keying). A tiny pre-middleware stashes a
    ``user_id`` on request.state (simulates the auth middleware for L3
    testing) based on the ``X-Test-User`` header."""
    app = FastAPI()

    # FastAPI/Starlette runs middleware in REVERSE registration order
    # (last added = outermost = runs first). We need user injection to
    # run BEFORE the rate limiter so request.state.user_id is populated
    # in time for L3 keying. Therefore: add the rate limiter first, the
    # injector second. Execution order ends up:
    #    inject_user → rate_limit → endpoint
    app.add_middleware(
        InMemoryRateLimitMiddleware,
        config=RateLimitConfig(
            enabled=True,
            requests_per_minute=l1_rpm,
            path_prefixes=("/chat",),
            thread_rpm=l2_rpm,
            user_rpm=l3_rpm,
            exempt_ips=exempt_ips,
        ),
    )

    # Simulate what the auth middleware does — set request.state.user_id
    # from a test-only header.
    @app.middleware("http")
    async def _inject_user(request, call_next):
        uid = request.headers.get("x-test-user")
        if uid:
            request.state.user_id = uid
        return await call_next(request)

    @app.post("/chat/echo")
    def echo(body: dict):
        return {"ok": True, "body": body}

    @app.get("/chat/ping")
    def ping():
        return {"ok": True}

    return app


class TestRateLimitTieredL2Thread:
    """L2 (per-thread_id) tier."""

    def test_thread_limit_blocks_before_ip_limit(self):
        """Thread-tier cap trips before IP-tier on concurrent
        same-thread requests."""
        app = _tiered_app(l1_rpm=100, l2_rpm=3)
        client = TestClient(app)
        # 3 requests on the same thread succeed.
        for _ in range(3):
            r = client.post("/chat/echo", json={"thread_id": "tab-1", "message": "hi"})
            assert r.status_code == 200
        # 4th trips L2 despite L1 being nowhere near its cap.
        r = client.post("/chat/echo", json={"thread_id": "tab-1", "message": "hi"})
        assert r.status_code == 429
        assert r.json()["tier"] == "thread"

    def test_different_threads_do_not_share_bucket(self):
        app = _tiered_app(l1_rpm=100, l2_rpm=2)
        client = TestClient(app)
        # Each thread has its own bucket.
        for _ in range(2):
            assert client.post("/chat/echo", json={"thread_id": "tab-a"}).status_code == 200
        for _ in range(2):
            assert client.post("/chat/echo", json={"thread_id": "tab-b"}).status_code == 200
        # Now each is at cap; one more on either trips L2.
        assert client.post("/chat/echo", json={"thread_id": "tab-a"}).status_code == 429

    def test_l2_off_when_rpm_zero(self):
        """thread_rpm=0 disables L2 entirely — only L1 applies."""
        app = _tiered_app(l1_rpm=5, l2_rpm=0)
        client = TestClient(app)
        for _ in range(5):
            r = client.post("/chat/echo", json={"thread_id": "tab-1"})
            assert r.status_code == 200
        # L1 trips at 6th. If L2 were still active with its default it'd
        # have fired earlier.
        r = client.post("/chat/echo", json={"thread_id": "tab-1"})
        assert r.status_code == 429
        assert r.json()["tier"] == "ip"

    def test_missing_thread_id_falls_through_to_ip(self):
        """A POST with no thread_id skips L2 silently; L1 still applies."""
        app = _tiered_app(l1_rpm=3, l2_rpm=2)
        client = TestClient(app)
        # Three requests with no thread_id — L2 can't key them so it's
        # skipped. L1 caps at 3, 4th trips L1.
        for _ in range(3):
            assert client.post("/chat/echo", json={"message": "no tid"}).status_code == 200
        r = client.post("/chat/echo", json={"message": "no tid"})
        assert r.status_code == 429
        assert r.json()["tier"] == "ip"


class TestRateLimitTieredL3UserStub:
    """L3 (per-user_id) stub — activates when request.state.user_id is set."""

    def test_user_limit_trips_with_auth_header_simulated(self):
        app = _tiered_app(l1_rpm=100, l3_rpm=2)
        client = TestClient(app)
        # Two requests under the same simulated user succeed.
        for _ in range(2):
            r = client.get("/chat/ping", headers={"X-Test-User": "alice"})
            assert r.status_code == 200
        r = client.get("/chat/ping", headers={"X-Test-User": "alice"})
        assert r.status_code == 429
        assert r.json()["tier"] == "user"

    def test_l3_stays_inert_when_no_user_id(self):
        """No auth header → no user_id → L3 can't key → tier silent.
        L1 is what protects the endpoint."""
        app = _tiered_app(l1_rpm=3, l3_rpm=1)
        client = TestClient(app)
        for _ in range(3):
            assert client.get("/chat/ping").status_code == 200
        r = client.get("/chat/ping")
        assert r.status_code == 429
        assert r.json()["tier"] == "ip"  # not 'user' — L3 never fired

    def test_different_users_do_not_share_bucket(self):
        app = _tiered_app(l1_rpm=100, l3_rpm=2)
        client = TestClient(app)
        for _ in range(2):
            assert client.get("/chat/ping", headers={"X-Test-User": "alice"}).status_code == 200
        for _ in range(2):
            assert client.get("/chat/ping", headers={"X-Test-User": "bob"}).status_code == 200
        r = client.get("/chat/ping", headers={"X-Test-User": "alice"})
        assert r.status_code == 429


class TestRateLimitExemptions:
    def test_exempt_ip_bypasses_all_tiers(self):
        """An exempt IP should never hit 429 regardless of tiers."""
        app = _tiered_app(
            l1_rpm=1, l2_rpm=1, l3_rpm=1,
            exempt_ips=frozenset({"testclient"}),  # FastAPI TestClient's client.host
        )
        client = TestClient(app)
        # Way over every tier; all must succeed.
        for _ in range(20):
            r = client.post("/chat/echo", json={"thread_id": "t", "message": "x"},
                            headers={"X-Test-User": "alice"})
            assert r.status_code == 200

    def test_non_exempt_ip_still_limited(self):
        """Exemption list doesn't silently disable the limiter for
        other IPs."""
        app = _tiered_app(
            l1_rpm=2,
            exempt_ips=frozenset({"10.99.99.99"}),  # not the test client's IP
        )
        client = TestClient(app)
        for _ in range(2):
            assert client.post("/chat/echo", json={"thread_id": "t"}).status_code == 200
        r = client.post("/chat/echo", json={"thread_id": "t"})
        assert r.status_code == 429


class TestRateLimitConfigTiered:
    def test_hosted_default_includes_l2_and_l3(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        monkeypatch.delenv("CHAT_RATE_LIMIT_PER_MINUTE", raising=False)
        monkeypatch.delenv("CHAT_RATE_LIMIT_THREAD_PER_MINUTE", raising=False)
        monkeypatch.delenv("CHAT_RATE_LIMIT_USER_PER_MINUTE", raising=False)
        cfg = resolve_rate_limit_config()
        assert cfg.enabled is True
        assert cfg.requests_per_minute == 30
        assert cfg.thread_rpm == 20
        assert cfg.user_rpm == 120

    def test_explicit_env_overrides_tiers(self, monkeypatch):
        monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "60")
        monkeypatch.setenv("CHAT_RATE_LIMIT_THREAD_PER_MINUTE", "5")
        monkeypatch.setenv("CHAT_RATE_LIMIT_USER_PER_MINUTE", "0")  # disable L3
        cfg = resolve_rate_limit_config()
        assert cfg.requests_per_minute == 60
        assert cfg.thread_rpm == 5
        assert cfg.user_rpm == 0

    def test_exempt_ips_parsed_from_env(self, monkeypatch):
        monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "30")
        monkeypatch.setenv("RATE_LIMIT_EXEMPT_IPS", "10.0.0.1, 10.0.0.2,  ,10.0.0.3")
        cfg = resolve_rate_limit_config()
        assert cfg.exempt_ips == frozenset({"10.0.0.1", "10.0.0.2", "10.0.0.3"})


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
