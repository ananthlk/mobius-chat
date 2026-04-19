"""Phase 2d — apply ``require_user`` to the 10 unprotected write endpoints.

Phase 1h shipped the ``require_user`` dependency + ``CHAT_AUTH_MODE``
gating, but only the task-manager proxy and the uploads router adopted
it. The audit flagged ten more POST endpoints with no auth:

  1. ``POST /chat``                              — main chat ingress
  2. ``POST /chat/roster-upload``                — file uploads
  3. ``POST /chat/qc-user-score/{id}``           — user-edited scores
  4. ``POST /chat/adjudication-feedback/{id}``   — QA thumbs
  5. ``POST /chat/feedback/{id}``                — turn-level thumbs
  6. ``POST /chat/llm-performance-feedback/{id}``— model-routing thumbs
  7. ``POST /chat/source-feedback/{id}``         — per-source thumbs
  8. ``POST /chat/doc-reader/read``              — doc-reader proxy
  9. ``POST /chat/doc-reader/extract``           — doc-reader proxy
 10. ``POST /chat/doc-reader/summarize``         — doc-reader proxy

Phase 2d applies ``Depends(require_user)`` to all ten. The unit tests
below lock that in: if someone removes the dependency from any endpoint,
the corresponding ``test_<endpoint>_requires_auth_in_hosted_mode`` fails.

**Not covered here (intentional):**

- ``POST /chat/qc-audit/{id}`` — guarded by its own
  ``MOBIUS_QC_AUDIT_SECRET`` header secret (service-to-service from the
  eval adjudicator, not browser). Adding user auth would break the
  eval harness.
- ``POST /internal/skill-llm`` — guarded by
  ``MOBIUS_SKILL_LLM_INTERNAL_KEY`` (service-to-service).
- ``POST /chat/org-name-candidates`` — read-only proxy to a skill;
  no side effects, no user-scoped data. Left unprotected because the
  attack surface is "waste a bit of latency on someone's NPPES
  search" rather than anything persistent.

**Test strategy:**

Two levels:

1. *Route introspection* — for each protected endpoint, assert that
   ``require_user`` appears in the FastAPI dependant tree. This is the
   regression guard — if someone deletes ``Depends(require_user)``,
   the introspection test catches it without requiring end-to-end
   request flow.

2. *Behavioral check via TestClient* — one test per endpoint that
   sets ``CHAT_AUTH_MODE=required`` and POSTs without an
   ``Authorization`` header, expecting 401. This is the
   "it actually works" test. We don't exercise the happy path here
   because that's covered by test_front_door.py's direct ``require_user``
   tests (we don't want to duplicate JWT-decoding test setup).

The two layers together mean a break in the wiring surfaces either as
a fast introspection failure OR as a 401-not-returned failure — whichever
angle the regression comes from.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


# ── Route introspection fixture ──────────────────────────────────────


@pytest.fixture(scope="module")
def app_routes() -> dict[tuple[str, str], Any]:
    """Return a dict keyed by (method, path) -> route object for the
    full app. Built once per module to avoid re-importing main.py on
    every test."""
    from app.main import app

    out: dict[tuple[str, str], Any] = {}
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        for m in methods:
            out[(m, path)] = route
    return out


def _route_has_require_user_dependency(route: Any) -> bool:
    """True if ``require_user`` is listed in this route's dependant chain.

    FastAPI stores dependencies on the ``dependant`` attribute; each
    dependency shows up as a sub-dependant with its ``.call``
    attribute pointing at the dependency function. We walk the tree
    and match by identity with the live ``require_user`` function."""
    from app.api.front_door import require_user

    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False

    seen: set[int] = set()
    stack = [dependant]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if getattr(node, "call", None) is require_user:
            return True
        stack.extend(getattr(node, "dependencies", None) or [])
    return False


# ── Route introspection: every protected endpoint has require_user ──


PROTECTED_ROUTES = [
    ("POST", "/chat"),
    ("POST", "/chat/roster-upload"),
    ("POST", "/chat/qc-user-score/{correlation_id}"),
    ("POST", "/chat/adjudication-feedback/{correlation_id}"),
    ("POST", "/chat/feedback/{correlation_id}"),
    ("POST", "/chat/llm-performance-feedback/{correlation_id}"),
    ("POST", "/chat/source-feedback/{correlation_id}"),
    ("POST", "/chat/doc-reader/read"),
    ("POST", "/chat/doc-reader/extract"),
    ("POST", "/chat/doc-reader/summarize"),
]


class TestProtectedRoutesHaveAuthDependency:
    @pytest.mark.parametrize("method,path", PROTECTED_ROUTES)
    def test_route_has_require_user(self, app_routes, method, path):
        """If this fails, someone deleted ``Depends(require_user)`` from
        the named endpoint. That's a security regression — the endpoint
        will silently accept requests without a JWT in hosted envs."""
        route = app_routes.get((method, path))
        assert route is not None, f"Route {method} {path} not mounted on app"
        assert _route_has_require_user_dependency(route), (
            f"Route {method} {path} is missing Depends(require_user). "
            "Phase 2d protected this endpoint; if you intentionally "
            "removed the dependency (e.g. making it service-to-service "
            "auth instead), also remove it from PROTECTED_ROUTES and "
            "document why in the docstring above."
        )


class TestExplicitlyNotProtectedRoutes:
    """Three write endpoints are deliberately NOT protected by
    ``require_user``. They use service-to-service auth OR have no
    side effects worth guarding. Lock that choice in so a future
    ``protect everything`` pass doesn't accidentally break the eval
    adjudicator or the credentialing-skill LLM proxy."""

    @pytest.mark.parametrize("method,path", [
        ("POST", "/chat/qc-audit/{correlation_id}"),
        ("POST", "/internal/skill-llm"),
        ("POST", "/chat/org-name-candidates"),
    ])
    def test_route_does_not_use_require_user(self, app_routes, method, path):
        route = app_routes.get((method, path))
        assert route is not None, f"Route {method} {path} not mounted on app"
        assert not _route_has_require_user_dependency(route), (
            f"Route {method} {path} now has Depends(require_user). "
            "If this is intentional, move it into PROTECTED_ROUTES and "
            "update the exclusion docstring in test_phase_2d_*.py. "
            "Heads-up: this endpoint uses service-to-service auth "
            "(MOBIUS_QC_AUDIT_SECRET / MOBIUS_SKILL_LLM_INTERNAL_KEY) "
            "or has no user-scoped state — adding user auth will "
            "break external callers."
        )


# ── Behavioral: 401 in hosted mode without JWT ───────────────────────


# These requests need a minimally-valid body so the auth check fires
# before any Pydantic validation. Map endpoint → (body, content_type).
# For multipart (/chat/roster-upload) we skip the behavioral test and
# rely on route introspection; TestClient multipart setup is noisy and
# adds nothing the introspection doesn't already cover.
_BEHAVIORAL_CASES = [
    ("/chat", {"message": "hi"}, "json"),
    ("/chat/qc-user-score/abc-123", {"user_score": 0.5}, "json"),
    ("/chat/adjudication-feedback/abc-123", {"rating": "up"}, "json"),
    ("/chat/feedback/abc-123", {"rating": "up"}, "json"),
    ("/chat/llm-performance-feedback/abc-123", {"rating": "up"}, "json"),
    ("/chat/source-feedback/abc-123", {"source_index": 1, "rating": "up"}, "json"),
    ("/chat/doc-reader/read", {"document_id": "x"}, "json"),
    ("/chat/doc-reader/extract", {"document_id": "x", "query": "?"}, "json"),
    ("/chat/doc-reader/summarize", {"document_id": "x"}, "json"),
]


@pytest.fixture
def hosted_auth_required_env(monkeypatch):
    """Simulate a hosted deploy with auth required. CHAT_AUTH_MODE
    overrides the CHAT_ENV default, so we don't have to set both and
    risk tripping the startup-assertion gate from Phase 2c."""
    monkeypatch.setenv("CHAT_AUTH_MODE", "required")
    yield


class TestHostedModeReturns401WithoutJwt:
    @pytest.mark.parametrize("path,body,ctype", _BEHAVIORAL_CASES)
    def test_post_without_jwt_is_401(
        self, hosted_auth_required_env, path, body, ctype,
    ):
        """Behavioral proof: with CHAT_AUTH_MODE=required, a POST that
        doesn't carry an Authorization header returns 401 — not 200,
        not 400, not 422. The body is intentionally minimal-valid so
        the auth check fires before Pydantic validation could produce
        a confusing 422 instead."""
        from app.main import app

        client = TestClient(app)
        resp = client.post(path, json=body)
        assert resp.status_code == 401, (
            f"{path} returned {resp.status_code} instead of 401 in hosted "
            f"mode without JWT. Response body: {resp.text[:300]}"
        )


class TestDevModeAllowsAnonymous:
    """With CHAT_AUTH_MODE=off (dev default), the same POSTs must NOT
    401 — the auth dependency is a no-op and the endpoint executes
    normally (though it may 400/422/500 for other reasons, that's
    fine — the test just asserts "not 401").

    Behavioral surface here is narrower than the hosted-mode 401 test
    because several endpoints hit upstream dependencies we don't want
    to stand up in a unit test (Redis queue for /chat; provider-roster
    HTTP for /chat/roster-upload; doc-reader skill for /chat/doc-reader/*).
    Those endpoints' "auth is off → endpoint reachable" behavior is
    covered by the route introspection class above — if the dependency
    is correctly configured for ``required`` it's also correctly
    configured for ``off`` (``auth_mode()`` is the switch both read).
    """

    # Endpoints that are self-contained enough to exercise without
    # standing up queue / doc-reader / provider-roster. The three
    # feedback endpoints that hit Postgres are included because the
    # storage layer gracefully no-ops when the DB URL is unset, which
    # is the default in this test env.
    _DEV_MODE_CASES = [
        (path, body, ctype) for (path, body, ctype) in _BEHAVIORAL_CASES
        if path not in {
            "/chat",                       # needs Redis queue
            "/chat/doc-reader/read",       # needs doc-reader skill
            "/chat/doc-reader/extract",
            "/chat/doc-reader/summarize",
        }
    ]

    @pytest.fixture
    def dev_auth_off_env(self, monkeypatch):
        monkeypatch.setenv("CHAT_AUTH_MODE", "off")
        yield

    @pytest.mark.parametrize("path,body,ctype", _DEV_MODE_CASES)
    def test_post_without_jwt_is_not_401_in_dev(
        self, dev_auth_off_env, path, body, ctype,
    ):
        from app.main import app

        client = TestClient(app)
        resp = client.post(path, json=body)
        # Endpoint may 400/422/500 for other reasons in test env
        # (no DB / no upstream skill) — that's fine. The guard is
        # specifically that the auth middleware isn't rejecting us.
        assert resp.status_code != 401, (
            f"{path} returned 401 in dev mode (CHAT_AUTH_MODE=off). "
            "The dependency should be a no-op when auth is disabled, "
            "but something in the auth chain is blocking."
        )
