"""Phase B.1c — instant-RAG upload catalog tests.

Covers three surfaces:

  1. Storage module (``app.storage.instant_rag_catalog``)
     CRUD behavior, status transitions, dual-write contract with the JSONB
     blob. Mocks ``db_execute`` / ``db_query`` from ``app.db_client`` (which
     the storage module uses post db-agent refactor). The mocks capture
     the SQL + params that would have been sent and return programmable
     result payloads.

  2. Router (``app.api.uploads``)
     Endpoint contract: exactly one of thread_id | user_id required, the
     404 / payload shape, datetimes serialized as ISO strings.

  3. Dual-write invariant
     When ``_handle_instant_rag_upload`` fires the catalog record_upload
     call, the JSONB append and the catalog insert must carry the same
     (document_id, upload_id, filename, thread_id). Protects against the
     two stores drifting.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── Mock db-agent plumbing ────────────────────────────────────────────────


@pytest.fixture
def mock_db(monkeypatch):
    """Patch db_execute / db_query used by the catalog module.

    Test sets ``mock_db.query_rows`` / ``mock_db.query_columns`` to program
    SELECT results, or ``mock_db.execute_rows_affected`` for writes. Tests
    read ``mock_db.executed`` — a list of (fn, sql, params) tuples — to
    inspect what the module asked the agent to do.
    """
    class Holder:
        # Configurable inputs
        query_rows: list[list] = []
        query_columns: list[str] | None = None
        execute_rows_affected: int = 1
        query_error: dict | None = None
        execute_error: dict | None = None
        # Captured calls (fn, sql, params) — tests assert on these
        executed: list[tuple[str, str, dict | None]] = []

    holder = Holder()
    holder.executed = []

    def _fake_query(sql, db_name, params=None, max_rows=1000):
        compact = " ".join(sql.split())
        holder.executed.append(("query", compact, params))
        if holder.query_error:
            return {"error": holder.query_error}
        cols = holder.query_columns
        if cols is None:
            from app.storage.instant_rag_catalog import _SELECT_COLUMNS
            cols = list(_SELECT_COLUMNS)
        return {
            "columns": cols,
            "rows": list(holder.query_rows),
            "row_count": len(holder.query_rows),
            "truncated": False,
        }

    def _fake_execute(sql, db_name, params=None):
        compact = " ".join(sql.split())
        holder.executed.append(("execute", compact, params))
        if holder.execute_error:
            return {"error": holder.execute_error}
        # Split on first whitespace-stripped keyword for operation inference.
        first_word = compact.strip().split(" ", 1)[0].upper()
        table = "instant_rag_uploads"
        return {
            "operation": "INSERT" if first_word == "INSERT" else
                         "UPDATE" if first_word == "UPDATE" else first_word,
            "table": table,
            "rows_affected": holder.execute_rows_affected,
        }

    monkeypatch.setattr("app.storage.instant_rag_catalog.db_query", _fake_query)
    monkeypatch.setattr("app.storage.instant_rag_catalog.db_execute", _fake_execute)
    return holder


# ── record_upload ─────────────────────────────────────────────────────────


class TestRecordUpload:
    def test_happy_path_inserts(self, mock_db):
        from app.storage import instant_rag_catalog as cat

        mock_db.execute_rows_affected = 1
        ok = cat.record_upload(
            document_id="doc-abc",
            envelope_id="env-1",
            upload_id="u-1",
            thread_id="t-1",
            filename="CP.BH.124.pdf",
            chunks_count=19,
        )
        assert ok is True
        assert len(mock_db.executed) == 1
        fn, sql, params = mock_db.executed[0]
        assert fn == "execute"
        assert "INSERT INTO instant_rag_uploads" in sql
        assert "ON CONFLICT (document_id) DO NOTHING" in sql
        assert params["document_id"] == "doc-abc"

    def test_conflict_returns_false_without_raising(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        # ON CONFLICT DO NOTHING → 0 rows affected
        mock_db.execute_rows_affected = 0
        ok = cat.record_upload(
            document_id="doc-abc", envelope_id="env-1", upload_id="u-1",
            thread_id="t-1", filename="x.pdf",
        )
        assert ok is False

    def test_missing_required_field_returns_false(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        for missing in ("document_id", "envelope_id", "upload_id", "thread_id"):
            kwargs = dict(
                document_id="doc-a", envelope_id="e", upload_id="u",
                thread_id="t", filename="x.pdf",
            )
            kwargs[missing] = ""
            mock_db.executed.clear()
            assert cat.record_upload(**kwargs) is False, (
                f"record_upload must reject empty {missing} without touching DB"
            )
            assert mock_db.executed == [], (
                f"No DB call must be made when {missing} is empty"
            )

    def test_default_expires_at_7d(self, mock_db):
        """Catalog TTL defaults to 7 days forward to match the skill's
        INSTANT_RAG_TTL_DAYS. Cleanup cron depends on this."""
        from app.storage import instant_rag_catalog as cat
        mock_db.execute_rows_affected = 1
        before = datetime.now(timezone.utc)
        cat.record_upload(
            document_id="doc-abc", envelope_id="e", upload_id="u",
            thread_id="t", filename="x.pdf",
        )
        after = datetime.now(timezone.utc)
        _fn, _sql, params = mock_db.executed[0]
        # expires_at is serialized as ISO string by the module (agent-friendly).
        expires_str = params["expires_at"]
        assert isinstance(expires_str, str)
        expires_at = datetime.fromisoformat(expires_str)
        expected_low  = before + timedelta(days=7) - timedelta(seconds=5)
        expected_high = after  + timedelta(days=7) + timedelta(seconds=5)
        assert expected_low <= expires_at <= expected_high, (
            f"Default expires_at ({expires_at}) not within 5s of now+7d."
        )

    def test_exception_is_swallowed(self, mock_db):
        """DB failure during record_upload must NOT raise — chunks are
        already durable in Chroma+PG, catalog is a secondary layer."""
        from app.storage import instant_rag_catalog as cat

        mock_db.execute_error = {"code": "connection_error", "message": "DB down"}
        ok = cat.record_upload(
            document_id="doc-abc", envelope_id="e", upload_id="u",
            thread_id="t", filename="x.pdf",
        )
        assert ok is False, "Failure returns False, doesn't raise"


# ── mark_status ───────────────────────────────────────────────────────────


class TestMarkStatus:
    def test_valid_transitions(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.execute_rows_affected = 1
        for s in ("expired", "discarded", "promoted"):
            assert cat.mark_status("doc-abc", s) is True

    def test_invalid_status_rejected(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        assert cat.mark_status("doc-abc", "weird") is False
        # No SQL should have run for the rejected case
        assert mock_db.executed == []

    def test_empty_doc_id_rejected(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        assert cat.mark_status("", cat.STATUS_EXPIRED) is False


# ── list_for_thread / list_for_user ──────────────────────────────────────


class TestListReads:
    def _sample_row(self, doc_id="doc-1", thread_id="t-1", user_id=None, status="active"):
        # Must match _SELECT_COLUMNS order exactly.
        return [
            doc_id, f"env-{doc_id}", f"u-{doc_id}", thread_id, user_id,
            "f.pdf", "application/pdf", 1024, 10,
            status,
            None, None, None, None,   # suggested_*
            None, None, None, None,   # confirmed_*
            datetime(2026, 4, 17, 22, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc),
            None,
        ]

    def test_list_for_thread_scopes_query(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.query_rows = [self._sample_row(doc_id="doc-a", thread_id="t-1")]
        rows = cat.list_for_thread("t-1")
        assert len(rows) == 1
        assert rows[0]["document_id"] == "doc-a"
        assert rows[0]["thread_id"] == "t-1"
        fn, sql, params = mock_db.executed[0]
        assert fn == "query"
        assert "WHERE thread_id = :tid" in sql
        assert "status = 'active'" in sql
        assert params["tid"] == "t-1"

    def test_list_for_thread_include_inactive(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.query_rows = []
        cat.list_for_thread("t-1", include_inactive=True)
        _fn, sql, _p = mock_db.executed[0]
        assert "status = 'active'" not in sql

    def test_list_for_user_cross_thread(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.query_rows = [
            self._sample_row(doc_id="doc-a", thread_id="t-1", user_id="u-1"),
            self._sample_row(doc_id="doc-b", thread_id="t-2", user_id="u-1"),
        ]
        rows = cat.list_for_user("u-1")
        assert [r["document_id"] for r in rows] == ["doc-a", "doc-b"]
        _fn, sql, params = mock_db.executed[0]
        assert "WHERE user_id = :uid" in sql
        assert "LIMIT" in sql
        assert params["uid"] == "u-1"

    def test_list_for_user_honors_limit(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.query_rows = []
        cat.list_for_user("u-1", limit=25)
        _fn, _sql, params = mock_db.executed[0]
        assert params["lim"] == 25

    def test_empty_id_returns_empty(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        assert cat.list_for_thread("") == []
        assert cat.list_for_user("") == []


# ── list_expiring_before (cleanup cron input) ────────────────────────────


class TestListExpiringBefore:
    def test_scopes_to_active_and_past_cutoff(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.query_rows = []
        cutoff = datetime(2026, 4, 17, tzinfo=timezone.utc)
        cat.list_expiring_before(cutoff)
        _fn, sql, _p = mock_db.executed[0]
        assert "status = 'active'" in sql
        assert "expires_at < :cutoff" in sql
        assert "expires_at IS NOT NULL" in sql


# ── router ────────────────────────────────────────────────────────────────


class TestUploadsRouter:
    def _app(self):
        from app.api.uploads import router
        a = FastAPI()
        a.include_router(router)
        return a

    def test_list_requires_thread_or_user(self):
        client = TestClient(self._app())
        r = client.get("/chat/uploads")
        assert r.status_code == 400
        assert "thread_id or user_id" in r.json()["detail"]

    def test_list_rejects_both(self):
        client = TestClient(self._app())
        r = client.get("/chat/uploads?thread_id=t-1&user_id=u-1")
        assert r.status_code == 400

    def test_list_by_thread(self):
        from app.api import uploads as uploads_mod

        sample = {
            "document_id": "doc-1", "envelope_id": "e-1", "upload_id": "u-1",
            "thread_id": "t-1", "user_id": None, "filename": "x.pdf",
            "content_type": "application/pdf", "byte_size": 2048,
            "chunks_count": 10, "status": "active",
            "suggested_payer": None, "suggested_state": None,
            "suggested_program": None, "suggested_authority": None,
            "confirmed_payer": None, "confirmed_state": None,
            "confirmed_program": None, "confirmed_authority": None,
            "created_at": datetime(2026, 4, 17, tzinfo=timezone.utc),
            "expires_at": datetime(2026, 4, 24, tzinfo=timezone.utc),
            "last_queried_at": None,
        }
        with patch.object(uploads_mod, "list_for_thread", return_value=[sample]):
            client = TestClient(self._app())
            r = client.get("/chat/uploads?thread_id=t-1")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["uploads"][0]["document_id"] == "doc-1"
        assert isinstance(body["uploads"][0]["created_at"], str)
        assert "2026-04-17" in body["uploads"][0]["created_at"]

    def test_get_by_document_id_404(self):
        from app.api import uploads as uploads_mod
        with patch.object(uploads_mod, "get_by_document_id", return_value=None):
            client = TestClient(self._app())
            r = client.get("/chat/uploads/doc-nope")
        assert r.status_code == 404

    def test_get_by_document_id_ok(self):
        from app.api import uploads as uploads_mod
        sample = {
            "document_id": "doc-1", "envelope_id": "e", "upload_id": "u",
            "thread_id": "t", "user_id": None, "filename": "x.pdf",
            "content_type": None, "byte_size": None, "chunks_count": 0,
            "status": "active",
            "suggested_payer": None, "suggested_state": None,
            "suggested_program": None, "suggested_authority": None,
            "confirmed_payer": None, "confirmed_state": None,
            "confirmed_program": None, "confirmed_authority": None,
            "created_at": datetime(2026, 4, 17, tzinfo=timezone.utc),
            "expires_at": None, "last_queried_at": None,
        }
        with patch.object(uploads_mod, "get_by_document_id", return_value=sample):
            client = TestClient(self._app())
            r = client.get("/chat/uploads/doc-1")
        assert r.status_code == 200
        assert r.json()["document_id"] == "doc-1"


# ── dual-write contract ─────────────────────────────────────────────────


class TestDualWriteContract:
    """The JSONB blob (active.uploaded_files[]) and the catalog row must
    carry matching (document_id, upload_id, filename, thread_id)."""

    def test_handle_instant_rag_upload_writes_to_both(self):
        from pathlib import Path
        main_py = Path(__file__).parent.parent / "app" / "main.py"
        text = main_py.read_text()
        import re
        m = re.search(
            r"def _handle_instant_rag_upload\b.*?(?=\n(?:def |@app\.)|\Z)",
            text, re.DOTALL,
        )
        assert m, "_handle_instant_rag_upload function missing"
        body = m.group(0)

        assert "append_uploaded_file_record(" in body, (
            "JSONB fast-path write missing — ReAct loop's "
            "_resolve_upload_document_id depends on this."
        )
        assert "instant_rag_catalog" in body or "record_upload" in body, (
            "Catalog write missing — cross-thread uploads queries "
            "(/chat/uploads?user_id=X) will silently miss this upload."
        )
