"""Phase 1b — extracted /chat feedback + QC router.

These tests do double duty:

1. **URL back-compat.** Every endpoint that used to live under
   ``@app.post`` in main.py must still respond at the same path.
2. **Postgres-persistence contract.** Per user request on 2026-04-17:
   "make sure each of these feedbacks are persisted in postgres."
   Each endpoint is asserted to call the corresponding ``app.storage``
   persistence function with the correct arguments. The storage layer
   itself is separately verified to run real ``INSERT … ON CONFLICT
   DO UPDATE`` against real migrations (003 chat_feedback, 006
   chat_source_feedback, 023 chat_turns.qc_audit, 024
   llm_performance_feedback, 025 adjudication_feedback) — see
   ``app/storage/feedback.py`` and ``app/storage/turns.py``.

Endpoints audited:
    POST /chat/feedback/{cid}                 → chat_feedback              (migration 003)
    POST /chat/source-feedback/{cid}          → chat_source_feedback       (migration 006)
    POST /chat/adjudication-feedback/{cid}    → adjudication_feedback      (migration 025)
    POST /chat/llm-performance-feedback/{cid} → llm_performance_feedback   (migration 024)
    POST /chat/qc-audit/{cid}                 → chat_turns.qc_audit JSONB  (migration 023)
    POST /chat/qc-user-score/{cid}            → chat_turns.qc_audit JSONB  (migration 023)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


def _app():
    """Minimal app that mounts only the feedback router."""
    from fastapi import FastAPI

    from app.api.feedback import router

    a = FastAPI()
    a.include_router(router)
    return a


# ── URL back-compat ─────────────────────────────────────────────────────────


class TestURLBackCompat:
    """Regression: every pre-1b path must still resolve at the same URL."""

    def test_feedback_path_exists(self):
        with (
            patch("app.api.feedback.insert_feedback") as _m,
            patch("app.api.feedback.get_queue"),
        ):
            r = TestClient(_app()).post(
                "/chat/feedback/abc123", json={"rating": "up"}
            )
        assert r.status_code == 200

    def test_source_feedback_path_exists(self):
        with patch("app.api.feedback.insert_source_feedback") as _m:
            r = TestClient(_app()).post(
                "/chat/source-feedback/abc123",
                json={"source_index": 1, "rating": "up"},
            )
        assert r.status_code == 200

    def test_adjudication_feedback_path_exists(self):
        with patch("app.api.feedback.insert_adjudication_feedback") as _m:
            r = TestClient(_app()).post(
                "/chat/adjudication-feedback/abc123", json={"rating": "up"}
            )
        assert r.status_code == 200

    def test_llm_performance_feedback_path_exists(self):
        with patch("app.api.feedback.insert_llm_performance_feedback") as _m:
            r = TestClient(_app()).post(
                "/chat/llm-performance-feedback/abc123", json={"rating": "up"}
            )
        assert r.status_code == 200


# ── Postgres persistence contract ──────────────────────────────────────────


class TestFeedbackPersistsToPostgres:
    """Each endpoint must invoke the corresponding insert_* fn that writes to PG."""

    def test_feedback_endpoint_calls_insert_feedback(self):
        with (
            patch("app.api.feedback.insert_feedback") as m,
            patch("app.api.feedback.get_queue"),
        ):
            TestClient(_app()).post(
                "/chat/feedback/cid-1", json={"rating": "up", "comment": "great"}
            )
        m.assert_called_once_with("cid-1", "up", "great")

    def test_source_feedback_endpoint_calls_insert_source_feedback(self):
        with patch("app.api.feedback.insert_source_feedback") as m:
            TestClient(_app()).post(
                "/chat/source-feedback/cid-2",
                json={"source_index": 3, "rating": "down"},
            )
        m.assert_called_once_with("cid-2", 3, "down")

    def test_adjudication_endpoint_calls_insert_adjudication_feedback(self):
        with patch("app.api.feedback.insert_adjudication_feedback") as m:
            TestClient(_app()).post(
                "/chat/adjudication-feedback/cid-3",
                json={"rating": "down", "comment": "wrong"},
            )
        m.assert_called_once_with("cid-3", "down", "wrong")

    def test_llm_performance_endpoint_calls_insert_llm_performance_feedback(self):
        with patch("app.api.feedback.insert_llm_performance_feedback") as m:
            TestClient(_app()).post(
                "/chat/llm-performance-feedback/cid-4",
                json={"rating": "up"},
            )
        m.assert_called_once_with("cid-4", "up", None)


class TestQcAuditPersistsToPostgres:
    def test_qc_audit_endpoint_updates_chat_turns_qc_audit(self):
        """qc-audit endpoint must UPDATE chat_turns.qc_audit (migration 023)."""
        with (
            patch("app.api.feedback.update_turn_qc_audit") as m_update,
            patch(
                "app.api.feedback.fetch_turn_qc_audit",
                return_value={"passed": True},
            ),
            patch("app.api.feedback.publish_quality_audit_event"),
            patch("app.api.feedback.get_queue"),
            patch.dict("os.environ", {"MOBIUS_QC_AUDIT_SECRET": ""}),
        ):
            r = TestClient(_app()).post(
                "/chat/qc-audit/cid-5",
                json={"passed": True, "reason": "looks good"},
            )
        assert r.status_code == 200
        m_update.assert_called_once()
        # First positional arg is correlation_id
        args = m_update.call_args.args
        assert args[0] == "cid-5"
        # Second positional is a dict with passed/reason/source/audited_at/automated_score
        persisted = args[1]
        assert persisted["passed"] is True
        assert persisted["reason"] == "looks good"
        assert persisted["automated_score"] == 1.0

    def test_qc_user_score_endpoint_updates_chat_turns_qc_audit(self):
        """qc-user-score endpoint persists the human-override score to the same JSONB column."""
        with (
            patch("app.api.feedback.update_turn_qc_audit") as m_update,
            patch(
                "app.api.feedback.fetch_turn_qc_audit",
                return_value={"user_score": 0.8},
            ),
            patch("app.api.feedback.get_queue"),
        ):
            r = TestClient(_app()).post(
                "/chat/qc-user-score/cid-6",
                json={"user_score": 0.8, "user_score_comment": "better than auto"},
            )
        assert r.status_code == 200
        m_update.assert_called_once()
        persisted = m_update.call_args.args[1]
        assert persisted["user_score"] == 0.8
        assert persisted["user_score_comment"] == "better than auto"


# ── Input validation preserved ──────────────────────────────────────────────


class TestInputValidation:
    """The endpoints still reject bad input the same way they did in main.py."""

    def test_rejects_invalid_rating(self):
        r = TestClient(_app()).post(
            "/chat/feedback/cid", json={"rating": "maybe"}
        )
        assert r.status_code == 400

    def test_rejects_invalid_source_index(self):
        r = TestClient(_app()).post(
            "/chat/source-feedback/cid",
            json={"source_index": 0, "rating": "up"},
        )
        assert r.status_code == 400

    def test_rejects_user_score_out_of_range(self):
        r = TestClient(_app()).post(
            "/chat/qc-user-score/cid", json={"user_score": 1.5}
        )
        assert r.status_code == 400

    def test_qc_audit_secret_enforced_when_set(self):
        """If MOBIUS_QC_AUDIT_SECRET is set, a wrong/missing header → 403."""
        with patch.dict(
            "os.environ", {"MOBIUS_QC_AUDIT_SECRET": "topsecret"}
        ):
            r = TestClient(_app()).post(
                "/chat/qc-audit/cid",
                json={"passed": True},
                headers={"X-Mobius-QC-Audit-Secret": "wrong"},
            )
        assert r.status_code == 403


# ── main.py hygiene ─────────────────────────────────────────────────────────


class TestMainPyHygiene:
    """Lock the refactor — the six feedback endpoints must NOT reappear in main.py."""

    def test_no_feedback_decorators_left_in_main_py(self):
        from pathlib import Path

        main_py = Path(__file__).parent.parent / "app" / "main.py"
        text = main_py.read_text()
        # Decorators that used to live here:
        forbidden = [
            "/chat/qc-audit/",
            "/chat/qc-user-score/",
            "/chat/adjudication-feedback/",
            '"/chat/feedback/',  # quoted to avoid false positives on the /chat/feedback wildcard
            "/chat/llm-performance-feedback/",
            "/chat/source-feedback/",
        ]
        for line in text.splitlines():
            if not line.strip().startswith("@app."):
                continue
            for f in forbidden:
                if f in line:
                    raise AssertionError(
                        f"Phase 1b regression — {f} endpoint back in main.py:\n  {line}"
                    )
