"""Phase 0.17 — feedback fail-closed on missing DB / missing table.

Regression tests for the Phase 1b audit findings:

1. All five ``insert_*`` fns in ``app/storage/feedback.py`` previously
   logged a WARNING and silently returned when ``CHAT_RAG_DATABASE_URL``
   was unset. Misconfigured production → feedback vanishes, no signal
   to ops.
2. ``insert_adjudication_feedback`` + ``insert_llm_performance_feedback``
   swallowed "relation does not exist" (migration 024/025 didn't run) at
   DEBUG level. Same silent-data-loss shape.

Phase 0.17 keeps dev ergonomics (missing DB or missing table is a warn
+ return) and makes staging/prod fail loudly: ``FeedbackPersistenceError``
propagates so FastAPI returns 500 to the caller.

Gate is ``CHAT_ENV``: ``dev`` (default) = degrade gracefully, anything
else = fail-closed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.storage.feedback import (
    FeedbackPersistenceError,
    _env_is_hosted,
    _handle_missing_db_url,
    _handle_missing_relation,
    insert_adjudication_feedback,
    insert_feedback,
    insert_llm_performance_feedback,
    insert_source_feedback,
)


# ── _env_is_hosted gate ─────────────────────────────────────────────────────


class TestEnvGate:
    def test_unset_defaults_to_dev(self, monkeypatch):
        monkeypatch.delenv("CHAT_ENV", raising=False)
        assert _env_is_hosted() is False

    @pytest.mark.parametrize("val", ["dev", "development", "local", "DEV", "Development"])
    def test_dev_variants_not_hosted(self, monkeypatch, val):
        monkeypatch.setenv("CHAT_ENV", val)
        assert _env_is_hosted() is False

    @pytest.mark.parametrize("val", ["staging", "prod", "production", "STAGING", "PROD"])
    def test_hosted_variants(self, monkeypatch, val):
        monkeypatch.setenv("CHAT_ENV", val)
        assert _env_is_hosted() is True

    def test_unknown_value_treated_as_hosted(self, monkeypatch):
        """Safer default: if someone sets CHAT_ENV=test123, assume hosted and fail loud."""
        monkeypatch.setenv("CHAT_ENV", "test123")
        assert _env_is_hosted() is True


# ── Missing DB URL handling ────────────────────────────────────────────────


class TestMissingDbUrl:
    def test_dev_logs_and_returns(self, monkeypatch, caplog):
        """Dev without Postgres → warn + return (no exception)."""
        monkeypatch.setenv("CHAT_ENV", "dev")
        import logging

        with caplog.at_level(logging.WARNING):
            _handle_missing_db_url("feedback")
        assert any(
            "CHAT_RAG_DATABASE_URL not set" in r.getMessage()
            for r in caplog.records
        )

    def test_staging_raises(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "staging")
        with pytest.raises(FeedbackPersistenceError) as exc:
            _handle_missing_db_url("feedback")
        assert "CHAT_RAG_DATABASE_URL not set" in str(exc.value)

    def test_prod_raises(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        with pytest.raises(FeedbackPersistenceError):
            _handle_missing_db_url("source feedback")


# ── Missing table / migration handling ─────────────────────────────────────


class TestMissingRelation:
    def test_dev_logs_with_migration_hint(self, monkeypatch, caplog):
        monkeypatch.setenv("CHAT_ENV", "dev")
        import logging

        err = RuntimeError("relation \"adjudication_feedback\" does not exist")
        with caplog.at_level(logging.WARNING):
            _handle_missing_relation("adjudication_feedback", "025", err)
        msg = caplog.records[-1].getMessage()
        assert "migration 025" in msg
        assert "adjudication_feedback" in msg

    def test_prod_raises_with_migration_hint(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        err = RuntimeError("relation \"llm_performance_feedback\" does not exist")
        with pytest.raises(FeedbackPersistenceError) as exc:
            _handle_missing_relation("llm_performance_feedback", "024", err)
        s = str(exc.value)
        assert "migration 024" in s
        assert "llm_performance_feedback" in s


# ── insert_* end-to-end behavior ───────────────────────────────────────────


class TestInsertFeedbackEndToEnd:
    """Each ``insert_*`` returns cleanly in dev without DB; raises in prod."""

    @pytest.fixture(autouse=True)
    def _mock_no_db_url(self, monkeypatch):
        """Force ``_get_db_url`` to return empty (simulates unset env)."""
        import app.storage.feedback as fb

        monkeypatch.setattr(fb, "_get_db_url", lambda: "")

    def test_feedback_dev_returns_silently(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "dev")
        insert_feedback("cid", "up", None)  # no exception

    def test_feedback_prod_raises(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        with pytest.raises(FeedbackPersistenceError):
            insert_feedback("cid", "up", None)

    def test_source_feedback_prod_raises(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        with pytest.raises(FeedbackPersistenceError):
            insert_source_feedback("cid", 1, "up")

    def test_adjudication_feedback_prod_raises(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        with pytest.raises(FeedbackPersistenceError):
            insert_adjudication_feedback("cid", "up", None)

    def test_llm_performance_feedback_prod_raises(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        with pytest.raises(FeedbackPersistenceError):
            insert_llm_performance_feedback("cid", "up", None)


# ── Back-compat: pre-0.17 callers don't regress in dev ─────────────────────


class TestBackwardsCompatibility:
    """Existing dev callers must keep working (the whole point of gating by env)."""

    def test_rating_validation_still_works_in_dev(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "dev")
        with pytest.raises(ValueError, match="rating must be"):
            insert_feedback("cid", "maybe", None)

    def test_rating_validation_still_works_in_prod(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "prod")
        with pytest.raises(ValueError, match="rating must be"):
            insert_feedback("cid", "sideways", None)

    def test_source_index_validation_still_works(self, monkeypatch):
        monkeypatch.setenv("CHAT_ENV", "dev")
        with pytest.raises(ValueError, match="source_index"):
            insert_source_feedback("cid", 0, "up")
