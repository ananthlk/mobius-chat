"""Phase 2c — startup assertion gate for hosted envs.

What this file locks in:

1. **Dev env is permissive.** ``assert_hosted_config()`` is a no-op when
   ``CHAT_ENV`` is unset, ``"dev"``, or any unknown value (matches
   front_door's permissive default — a typo shouldn't brick staging).

2. **Hosted env fails fast on missing DB URL.** ``CHAT_ENV=staging``
   or ``"prod"`` + no ``CHAT_RAG_DATABASE_URL`` (or fallback aliases)
   raises ``StartupAssertionError`` with the exact var name in the
   message. Catches the real deploy-time footgun: a hosted worker
   running without persistence would look healthy for seconds and
   then silently lose every thread.

3. **Hosted env fails fast on placeholder VERTEX_PROJECT_ID.** The
   ``"mobiusos-new"`` default sprinkled across chat_config.py and
   llm_provider.py will silently send prod traffic to the dev sandbox
   project if the env var isn't set. The gate rejects that at boot.

4. **Hosted env fails on auth-URL-without-secret.** If
   ``MOBIUS_OS_AUTH_URL`` is set, ``JWT_SECRET`` must also be set —
   otherwise every request fails at the auth middleware, but only
   after users start hitting ``/chat``. Fail at boot instead.

5. **All three fallback keys work for DB URL.** ``CHAT_RAG_DATABASE_URL``,
   ``RAG_DATABASE_URL``, ``CHAT_DATABASE_URL`` — any one satisfies the
   gate. Tested because the three-way fallback is a real thing in
   existing .env files and breaking it would be a nasty surprise.

6. **Error messages are actionable.** The raised message names the
   specific env var that's missing and gives a corrected-example
   hint, so the operator doesn't need to grep docs to fix it.

Not tested here:
  - MCP auto-register startup wiring (separate test file covers the
    adapter itself; the ``@app.on_event("startup")`` hook that calls
    it is tested by booting the app and checking the log line, which
    is out of scope for a unit test file).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from app.config import (
    StartupAssertionError,
    assert_hosted_config,
    chat_rag_database_url,
    resolved_vertex_project_id,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _env(**kwargs: str | None) -> dict[str, str]:
    """Build an env dict for patch.dict, stripping None values. Pass
    ``VAR=None`` to mean "delete this var for the test" — patch.dict
    with clear=False won't delete, so we simulate deletion by patching
    to empty and letting the code's ``.strip()`` treat it as unset."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _clear_env_vars(monkeypatch, *names: str) -> None:
    for n in names:
        monkeypatch.delenv(n, raising=False)


# ── Dev env: permissive ──────────────────────────────────────────────


class TestDevEnvIsNoOp:
    def test_chat_env_unset_is_dev(self, monkeypatch):
        """Default env should be treated as dev → assertion is a no-op."""
        _clear_env_vars(monkeypatch, "CHAT_ENV", "CHAT_RAG_DATABASE_URL",
                        "VERTEX_PROJECT_ID", "CHAT_VERTEX_PROJECT_ID")
        # Should not raise even with everything else unset.
        assert_hosted_config()  # expect no exception

    def test_chat_env_dev_explicit(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "dev")
        _clear_env_vars(monkeypatch, "CHAT_RAG_DATABASE_URL",
                        "VERTEX_PROJECT_ID", "CHAT_VERTEX_PROJECT_ID")
        assert_hosted_config()

    def test_unknown_chat_env_treated_as_dev(self, monkeypatch):
        """front_door falls back to dev when CHAT_ENV is unrecognized.
        Assertion must mirror that so typos don't brick anything."""
        monkeypatch.setenv("CHAT_ENV", "production")  # wrong value
        _clear_env_vars(monkeypatch, "CHAT_RAG_DATABASE_URL",
                        "VERTEX_PROJECT_ID", "CHAT_VERTEX_PROJECT_ID")
        assert_hosted_config()  # no exception — front_door treats this as dev


# ── Hosted env: fail-fast on missing DB URL ──────────────────────────


class TestHostedEnvDatabaseUrl:
    def test_staging_without_db_url_raises(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "staging")
        _clear_env_vars(
            monkeypatch,
            "CHAT_RAG_DATABASE_URL",
            "RAG_DATABASE_URL",
            "CHAT_DATABASE_URL",
        )
        # Provide the other required vars so we isolate the DB failure:
        monkeypatch.setenv("VERTEX_PROJECT_ID", "real-project")
        with pytest.raises(StartupAssertionError) as exc_info:
            assert_hosted_config()
        msg = str(exc_info.value)
        assert "CHAT_RAG_DATABASE_URL" in msg
        assert "staging" in msg

    def test_prod_without_db_url_raises(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        _clear_env_vars(
            monkeypatch,
            "CHAT_RAG_DATABASE_URL",
            "RAG_DATABASE_URL",
            "CHAT_DATABASE_URL",
        )
        monkeypatch.setenv("VERTEX_PROJECT_ID", "real-project")
        with pytest.raises(StartupAssertionError) as exc_info:
            assert_hosted_config()
        assert "CHAT_RAG_DATABASE_URL" in str(exc_info.value)
        assert "prod" in str(exc_info.value)

    def test_legacy_rag_database_url_satisfies_gate(self, monkeypatch):
        """Some older .env files use RAG_DATABASE_URL. The gate must
        accept it as a fallback or we break back-compat."""
        monkeypatch.setenv("CHAT_ENV", "staging")
        _clear_env_vars(monkeypatch, "CHAT_RAG_DATABASE_URL", "CHAT_DATABASE_URL")
        monkeypatch.setenv("RAG_DATABASE_URL", "postgresql://localhost/test")
        monkeypatch.setenv("VERTEX_PROJECT_ID", "real-project")
        # No exception:
        assert_hosted_config()

    def test_even_older_chat_database_url_satisfies_gate(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "staging")
        _clear_env_vars(monkeypatch, "CHAT_RAG_DATABASE_URL", "RAG_DATABASE_URL")
        monkeypatch.setenv("CHAT_DATABASE_URL", "postgresql://localhost/test")
        monkeypatch.setenv("VERTEX_PROJECT_ID", "real-project")
        assert_hosted_config()

    def test_canonical_chat_rag_database_url_satisfies_gate(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "staging")
        _clear_env_vars(monkeypatch, "RAG_DATABASE_URL", "CHAT_DATABASE_URL")
        monkeypatch.setenv("CHAT_RAG_DATABASE_URL", "postgresql://localhost/test")
        monkeypatch.setenv("VERTEX_PROJECT_ID", "real-project")
        assert_hosted_config()

    def test_whitespace_only_db_url_is_rejected(self, monkeypatch):
        """'   ' should count as unset — strip before checking."""
        monkeypatch.setenv("CHAT_ENV", "staging")
        monkeypatch.setenv("CHAT_RAG_DATABASE_URL", "   ")
        _clear_env_vars(monkeypatch, "RAG_DATABASE_URL", "CHAT_DATABASE_URL")
        monkeypatch.setenv("VERTEX_PROJECT_ID", "real-project")
        with pytest.raises(StartupAssertionError):
            assert_hosted_config()


# ── Hosted env: fail-fast on placeholder VERTEX_PROJECT_ID ───────────


class TestHostedEnvVertexProject:
    def test_staging_without_vertex_project_raises(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "staging")
        monkeypatch.setenv("CHAT_RAG_DATABASE_URL", "postgresql://localhost/t")
        _clear_env_vars(monkeypatch, "VERTEX_PROJECT_ID", "CHAT_VERTEX_PROJECT_ID")
        with pytest.raises(StartupAssertionError) as exc_info:
            assert_hosted_config()
        assert "VERTEX_PROJECT_ID" in str(exc_info.value)

    def test_placeholder_project_rejected_in_staging(self, monkeypatch):
        """The 'mobiusos-new' placeholder is the real footgun: env-unset
        looks like a hard error (obvious), but placeholder-set looks
        like success until the first Vertex call hits the wrong
        project. Reject at boot."""
        monkeypatch.setenv("CHAT_ENV", "staging")
        monkeypatch.setenv("CHAT_RAG_DATABASE_URL", "postgresql://localhost/t")
        monkeypatch.setenv("VERTEX_PROJECT_ID", "mobiusos-new")
        with pytest.raises(StartupAssertionError) as exc_info:
            assert_hosted_config()
        msg = str(exc_info.value)
        assert "mobiusos-new" in msg
        assert "placeholder" in msg.lower()

    def test_placeholder_project_rejected_in_prod(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        monkeypatch.setenv("CHAT_RAG_DATABASE_URL", "postgresql://localhost/t")
        monkeypatch.setenv("VERTEX_PROJECT_ID", "mobiusos-new")
        with pytest.raises(StartupAssertionError):
            assert_hosted_config()

    def test_real_project_satisfies_gate(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "staging")
        monkeypatch.setenv("CHAT_RAG_DATABASE_URL", "postgresql://localhost/t")
        monkeypatch.setenv("VERTEX_PROJECT_ID", "my-real-project-prod")
        _clear_env_vars(monkeypatch, "CHAT_VERTEX_PROJECT_ID")
        assert_hosted_config()  # no exception

    def test_chat_vertex_project_id_alias_satisfies_gate(self, monkeypatch):
        """CHAT_VERTEX_PROJECT_ID is a legacy alias some .env files use.
        Accept it as a fallback like the audit found — the 2-way
        fallback exists in llm_provider / embedding_provider."""
        monkeypatch.setenv("CHAT_ENV", "staging")
        monkeypatch.setenv("CHAT_RAG_DATABASE_URL", "postgresql://localhost/t")
        _clear_env_vars(monkeypatch, "VERTEX_PROJECT_ID")
        monkeypatch.setenv("CHAT_VERTEX_PROJECT_ID", "my-real-project")
        assert_hosted_config()

    def test_placeholder_allowed_in_dev(self, monkeypatch):
        """Dev is allowed to use the placeholder — it's the intended
        value for local dev (mobiusos-new is the dev sandbox project)."""
        monkeypatch.setenv("CHAT_ENV", "dev")
        monkeypatch.setenv("VERTEX_PROJECT_ID", "mobiusos-new")
        assert_hosted_config()  # no exception


# ── Hosted env: auth URL requires JWT secret ──────────────────────────


class TestHostedEnvAuthCoupling:
    def test_auth_url_without_secret_raises(self, monkeypatch):
        """Catches the misconfig where an operator enables Mobius-OS
        auth but forgets to set the shared JWT secret. Without this
        gate, every /chat request 401s — but only after users hit it."""
        monkeypatch.setenv("CHAT_ENV", "staging")
        monkeypatch.setenv("CHAT_RAG_DATABASE_URL", "postgresql://localhost/t")
        monkeypatch.setenv("VERTEX_PROJECT_ID", "real-project")
        monkeypatch.setenv("MOBIUS_OS_AUTH_URL", "https://auth.example.com")
        _clear_env_vars(monkeypatch, "JWT_SECRET")
        with pytest.raises(StartupAssertionError) as exc_info:
            assert_hosted_config()
        msg = str(exc_info.value)
        assert "MOBIUS_OS_AUTH_URL" in msg
        assert "JWT_SECRET" in msg

    def test_auth_url_with_secret_passes(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "staging")
        monkeypatch.setenv("CHAT_RAG_DATABASE_URL", "postgresql://localhost/t")
        monkeypatch.setenv("VERTEX_PROJECT_ID", "real-project")
        monkeypatch.setenv("MOBIUS_OS_AUTH_URL", "https://auth.example.com")
        monkeypatch.setenv("JWT_SECRET", "the-shared-secret")
        assert_hosted_config()

    def test_no_auth_url_skips_jwt_check(self, monkeypatch):
        """If auth isn't enabled, JWT_SECRET is legitimately optional."""
        monkeypatch.setenv("CHAT_ENV", "staging")
        monkeypatch.setenv("CHAT_RAG_DATABASE_URL", "postgresql://localhost/t")
        monkeypatch.setenv("VERTEX_PROJECT_ID", "real-project")
        _clear_env_vars(monkeypatch, "MOBIUS_OS_AUTH_URL", "JWT_SECRET")
        assert_hosted_config()


# ── Multi-problem reporting ──────────────────────────────────────────


class TestErrorMessageQuality:
    def test_multiple_problems_all_reported(self, monkeypatch):
        """When several env vars are wrong, the operator shouldn't have
        to fix them one at a time + restart repeatedly. The error
        message lists every problem in one pass."""
        monkeypatch.setenv("CHAT_ENV", "prod")
        _clear_env_vars(
            monkeypatch,
            "CHAT_RAG_DATABASE_URL",
            "RAG_DATABASE_URL",
            "CHAT_DATABASE_URL",
            "VERTEX_PROJECT_ID",
            "CHAT_VERTEX_PROJECT_ID",
        )
        with pytest.raises(StartupAssertionError) as exc_info:
            assert_hosted_config()
        msg = str(exc_info.value)
        # Both problems should surface in the same message:
        assert "CHAT_RAG_DATABASE_URL" in msg
        assert "VERTEX_PROJECT_ID" in msg
        # And the header summarizes count:
        assert "2 config problem" in msg.lower() or "2 " in msg

    def test_message_hints_at_dev_override(self, monkeypatch):
        """Message should point developers at the ``CHAT_ENV=dev``
        escape hatch when they're confused why their laptop won't
        boot with hosted env set."""
        monkeypatch.setenv("CHAT_ENV", "staging")
        _clear_env_vars(
            monkeypatch,
            "CHAT_RAG_DATABASE_URL",
            "RAG_DATABASE_URL",
            "CHAT_DATABASE_URL",
        )
        monkeypatch.setenv("VERTEX_PROJECT_ID", "real")
        with pytest.raises(StartupAssertionError) as exc_info:
            assert_hosted_config()
        msg = str(exc_info.value)
        assert "CHAT_ENV=dev" in msg


# ── Helper functions exposed by the config module ────────────────────


class TestConfigHelpers:
    def test_chat_rag_database_url_prefers_canonical(self, monkeypatch):
        monkeypatch.setenv("CHAT_RAG_DATABASE_URL", "canonical")
        monkeypatch.setenv("RAG_DATABASE_URL", "legacy")
        monkeypatch.setenv("CHAT_DATABASE_URL", "ancient")
        assert chat_rag_database_url() == "canonical"

    def test_chat_rag_database_url_falls_through(self, monkeypatch):
        _clear_env_vars(monkeypatch, "CHAT_RAG_DATABASE_URL")
        monkeypatch.setenv("RAG_DATABASE_URL", "legacy")
        assert chat_rag_database_url() == "legacy"

    def test_chat_rag_database_url_returns_empty_when_nothing_set(self, monkeypatch):
        _clear_env_vars(
            monkeypatch,
            "CHAT_RAG_DATABASE_URL",
            "RAG_DATABASE_URL",
            "CHAT_DATABASE_URL",
        )
        assert chat_rag_database_url() == ""

    def test_resolved_vertex_project_id_prefers_canonical(self, monkeypatch):
        monkeypatch.setenv("VERTEX_PROJECT_ID", "canonical")
        monkeypatch.setenv("CHAT_VERTEX_PROJECT_ID", "legacy")
        assert resolved_vertex_project_id() == "canonical"

    def test_resolved_vertex_project_id_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("VERTEX_PROJECT_ID", "  spacey  ")
        assert resolved_vertex_project_id() == "spacey"
