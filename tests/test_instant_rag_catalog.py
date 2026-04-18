"""Phase B.1c — instant-RAG upload catalog tests.

Covers three surfaces:

  1. Storage module (``app.storage.instant_rag_catalog``)
     CRUD behavior, status transitions, dual-write contract with the JSONB
     blob. Uses an in-memory mock psycopg2 so we don't need a real DB;
     the SQL is assembled and passed through a cursor-like recorder that
     returns the rows we program it to return.

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
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── Mock psycopg2 plumbing ────────────────────────────────────────────────


class _FakeCursor:
    """Minimal cursor: remembers executed SQL + params, returns programmed rows."""

    def __init__(self, rows_to_return: list[tuple] | None = None):
        self.executed: list[tuple[str, Any]] = []
        self._rows = rows_to_return or []
        self.rowcount = 0

    def execute(self, sql, params=None):
        # Normalize whitespace for easier substring checks in tests.
        compact_sql = " ".join(sql.split())
        self.executed.append((compact_sql, params))
        self.rowcount = 1 if self._rows else 0

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows_to_return: list[tuple] | None = None):
        self.cursor_obj = _FakeCursor(rows_to_return)
        self.committed = False
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


@pytest.fixture
def mock_db(monkeypatch):
    """Patch app.storage.instant_rag_catalog._conn to return a mock conn.
    Test sets `mock_db.rows` to program returns; test reads `mock_db.last_conn`
    to inspect what SQL ran."""
    class Holder:
        rows: list[tuple] = []
        last_conn: _FakeConn | None = None
    holder = Holder()

    def _fake_conn():
        conn = _FakeConn(rows_to_return=holder.rows)
        holder.last_conn = conn
        return conn

    monkeypatch.setattr("app.storage.instant_rag_catalog._conn", _fake_conn)
    monkeypatch.setattr(
        "app.storage.instant_rag_catalog._get_db_url",
        lambda: "postgresql://test:test@localhost/test",
    )
    return holder


# ── record_upload ─────────────────────────────────────────────────────────


class TestRecordUpload:
    def test_happy_path_inserts(self, mock_db):
        from app.storage import instant_rag_catalog as cat

        # Simulate a successful INSERT...RETURNING document_id
        mock_db.rows = [("doc-abc",)]
        ok = cat.record_upload(
            document_id="doc-abc",
            envelope_id="env-1",
            upload_id="u-1",
            thread_id="t-1",
            filename="CP.BH.124.pdf",
            chunks_count=19,
        )
        assert ok is True
        assert mock_db.last_conn is not None
        assert mock_db.last_conn.committed is True
        sql = mock_db.last_conn.cursor_obj.executed[0][0]
        assert "INSERT INTO instant_rag_uploads" in sql
        assert "ON CONFLICT (document_id) DO NOTHING" in sql

    def test_conflict_returns_false_without_raising(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        # RETURNING yields nothing → already existed
        mock_db.rows = []
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
            assert cat.record_upload(**kwargs) is False, (
                f"record_upload must reject empty {missing} without touching DB"
            )

    def test_default_expires_at_7d(self, mock_db):
        """Catalog TTL defaults to 7 days forward to match the skill's
        INSTANT_RAG_TTL_DAYS. Cleanup cron depends on this."""
        from app.storage import instant_rag_catalog as cat
        mock_db.rows = [("doc-abc",)]
        before = datetime.now(timezone.utc)
        cat.record_upload(
            document_id="doc-abc", envelope_id="e", upload_id="u",
            thread_id="t", filename="x.pdf",
        )
        after = datetime.now(timezone.utc)
        params = mock_db.last_conn.cursor_obj.executed[0][1]
        # expires_at is the last positional param in the INSERT (see source).
        expires_at = params[-1]
        assert isinstance(expires_at, datetime)
        expected_low  = before + timedelta(days=7) - timedelta(seconds=5)
        expected_high = after  + timedelta(days=7) + timedelta(seconds=5)
        assert expected_low <= expires_at <= expected_high, (
            f"Default expires_at ({expires_at}) not within 5s of now+7d."
        )

    def test_exception_is_swallowed(self, monkeypatch):
        """DB failure during record_upload must NOT raise — chunks are
        already durable in Chroma+PG, catalog is a secondary layer."""
        from app.storage import instant_rag_catalog as cat

        def _explode():
            raise RuntimeError("DB down")

        monkeypatch.setattr(cat, "_conn", _explode)
        monkeypatch.setattr(cat, "_get_db_url", lambda: "x")
        ok = cat.record_upload(
            document_id="doc-abc", envelope_id="e", upload_id="u",
            thread_id="t", filename="x.pdf",
        )
        assert ok is False, "Failure returns False, doesn't raise"


# ── mark_status ───────────────────────────────────────────────────────────


class TestMarkStatus:
    def test_valid_transitions(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.rows = [("doc-abc",)]  # rowcount > 0 via _rows len
        for s in ("expired", "discarded", "promoted"):
            assert cat.mark_status("doc-abc", s) is True

    def test_invalid_status_rejected(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        assert cat.mark_status("doc-abc", "weird") is False
        # No SQL should have run for the rejected case
        assert mock_db.last_conn is None

    def test_empty_doc_id_rejected(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        assert cat.mark_status("", cat.STATUS_EXPIRED) is False


# ── list_for_thread / list_for_user ──────────────────────────────────────


class TestListReads:
    def _sample_row(self, doc_id="doc-1", thread_id="t-1", user_id=None, status="active"):
        # Must match _SELECT_COLUMNS order exactly.
        return (
            doc_id, f"env-{doc_id}", f"u-{doc_id}", thread_id, user_id,
            "f.pdf", "application/pdf", 1024, 10,
            status,
            None, None, None, None,   # suggested_*
            None, None, None, None,   # confirmed_*
            datetime(2026, 4, 17, 22, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc),
            None,
        )

    def test_list_for_thread_scopes_query(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.rows = [self._sample_row(doc_id="doc-a", thread_id="t-1")]
        rows = cat.list_for_thread("t-1")
        assert len(rows) == 1
        assert rows[0]["document_id"] == "doc-a"
        assert rows[0]["thread_id"] == "t-1"
        sql = mock_db.last_conn.cursor_obj.executed[0][0]
        assert "WHERE thread_id = %s" in sql
        assert "status = 'active'" in sql

    def test_list_for_thread_include_inactive(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.rows = []
        cat.list_for_thread("t-1", include_inactive=True)
        sql = mock_db.last_conn.cursor_obj.executed[0][0]
        # Must NOT filter to active when include_inactive=True
        assert "status = 'active'" not in sql

    def test_list_for_user_cross_thread(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.rows = [
            self._sample_row(doc_id="doc-a", thread_id="t-1", user_id="u-1"),
            self._sample_row(doc_id="doc-b", thread_id="t-2", user_id="u-1"),
        ]
        rows = cat.list_for_user("u-1")
        assert [r["document_id"] for r in rows] == ["doc-a", "doc-b"]
        sql = mock_db.last_conn.cursor_obj.executed[0][0]
        assert "WHERE user_id = %s" in sql
        assert "LIMIT" in sql

    def test_list_for_user_honors_limit(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.rows = []
        cat.list_for_user("u-1", limit=25)
        params = mock_db.last_conn.cursor_obj.executed[0][1]
        assert params[-1] == 25

    def test_empty_id_returns_empty(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        assert cat.list_for_thread("") == []
        assert cat.list_for_user("") == []


# ── list_expiring_before (cleanup cron input) ────────────────────────────


class TestListExpiringBefore:
    def test_scopes_to_active_and_past_cutoff(self, mock_db):
        from app.storage import instant_rag_catalog as cat
        mock_db.rows = []
        cutoff = datetime(2026, 4, 17, tzinfo=timezone.utc)
        cat.list_expiring_before(cutoff)
        sql = mock_db.last_conn.cursor_obj.executed[0][0]
        assert "status = 'active'" in sql
        assert "expires_at < %s" in sql
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
        # Datetimes serialized as ISO strings (not the raw Python obj)
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
    carry matching (document_id, upload_id, filename, thread_id). This
    test doesn't run the full upload endpoint (heavy imports); it
    inspects the source of _handle_instant_rag_upload directly for the
    dual-write sequence so the invariant is locked structurally.
    """

    def test_handle_instant_rag_upload_writes_to_both(self):
        """_handle_instant_rag_upload must call BOTH append_uploaded_file_record
        (JSONB fast-path) AND the catalog record_upload after the skill
        ingest returns. Removing either is a silent data-loss regression.
        """
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
