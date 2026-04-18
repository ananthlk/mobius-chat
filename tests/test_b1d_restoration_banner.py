"""Phase B.1d — restoration banner + cross-thread upload link.

The 2026-04-17 live test exposed the thread-orphaning UX: user
uploads a PDF on thread A, hard-refreshes the browser, lands on a
new thread B, can't find the doc. B.1c made the catalog durable;
B.1d turns that durability into visible user value:

  - GET /chat/uploads/recent/for-restoration
      Powers the banner. Returns the user's recent uploads not already
      on the current thread. Auth-aware (user-scoped when authed,
      global in dev/auth-off).

  - POST /chat/uploads/{document_id}/link-to-thread
      Attaches an existing upload to another thread without
      re-uploading bytes. Writes a JSONB reference into target
      thread's active.uploaded_files[]. Enforces ownership when
      auth=required.

Surface tests here cover endpoint contract. Frontend structural
guards live in test_composer_attach.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _sample_upload_row(
    doc_id: str = "doc-1",
    thread_id: str = "t-origin",
    filename: str = "f.pdf",
    status: str = "active",
    user_id: str | None = None,
) -> dict:
    return {
        "document_id": doc_id,
        "envelope_id": f"env-{doc_id}",
        "upload_id": f"u-{doc_id}",
        "thread_id": thread_id,
        "user_id": user_id,
        "filename": filename,
        "content_type": "application/pdf",
        "byte_size": 2048,
        "chunks_count": 9,
        "status": status,
        "suggested_payer": None, "suggested_state": None,
        "suggested_program": None, "suggested_authority": None,
        "confirmed_payer": None, "confirmed_state": None,
        "confirmed_program": None, "confirmed_authority": None,
        "created_at": datetime(2026, 4, 17, 22, 0, tzinfo=timezone.utc),
        "expires_at":  datetime(2026, 4, 24, 22, 0, tzinfo=timezone.utc),
        "last_queried_at": None,
    }


@pytest.fixture
def app(monkeypatch):
    """App with just the uploads router — avoids pulling all of main.py
    (which would drag the ReAct pipeline + DB pool setup in).
    """
    from app.api.uploads import router
    monkeypatch.setenv("CHAT_AUTH_MODE", "off")  # dev default for tests
    a = FastAPI()
    a.include_router(router)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


# ── /chat/uploads/recent/for-restoration ─────────────────────────────────


class TestListRecentForRestoration:
    def test_auth_off_returns_globally_recent(self, client, monkeypatch):
        """auth=off (dev) → endpoint returns globally most-recent active
        uploads. Since require_user resolves to None, the route falls
        into the cursor-level SQL branch."""
        from app.api import uploads as uploads_mod

        # Mock the inner DB connection used by the dev branch so we don't
        # need a live PG for this test.
        fake_rows = [
            ("doc-1", "e-1", "u-1", "t-a", None, "A.pdf", None, None, 9, "active",
             None, None, None, None, None, None, None, None,
             datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc),
             datetime(2026, 4, 25, 0, 0, tzinfo=timezone.utc), None),
            ("doc-2", "e-2", "u-2", "t-b", None, "B.pdf", None, None, 5, "active",
             None, None, None, None, None, None, None, None,
             datetime(2026, 4, 17, 0, 0, tzinfo=timezone.utc),
             datetime(2026, 4, 24, 0, 0, tzinfo=timezone.utc), None),
        ]

        class FakeCur:
            def __init__(self): self._rows = fake_rows
            def execute(self, sql, params=None): pass
            def fetchall(self): return self._rows
            def close(self): pass

        class FakeConn:
            def cursor(self): return FakeCur()
            def close(self): pass

        monkeypatch.setattr("app.storage.instant_rag_catalog._conn", lambda: FakeConn())

        r = client.get("/chat/uploads/recent/for-restoration?limit=5")
        assert r.status_code == 200
        body = r.json()
        assert body["auth_scope"] == "global"
        assert body["count"] == 2
        names = [u["filename"] for u in body["uploads"]]
        assert "A.pdf" in names and "B.pdf" in names

    def test_excludes_current_thread(self, client, monkeypatch):
        """When current_thread_id is provided, uploads from that thread
        must be filtered out — the banner should only offer things to
        restore, not things already visible."""
        fake_rows = [
            ("doc-a", "e", "u-a", "t-current", None, "onthread.pdf", None, None, 1, "active",
             None, None, None, None, None, None, None, None,
             datetime(2026, 4, 18, tzinfo=timezone.utc), None, None),
            ("doc-b", "e", "u-b", "t-other",   None, "elsewhere.pdf", None, None, 1, "active",
             None, None, None, None, None, None, None, None,
             datetime(2026, 4, 18, tzinfo=timezone.utc), None, None),
        ]

        class FakeCur:
            def execute(self, sql, params=None): pass
            def fetchall(self): return fake_rows
            def close(self): pass

        class FakeConn:
            def cursor(self): return FakeCur()
            def close(self): pass

        monkeypatch.setattr("app.storage.instant_rag_catalog._conn", lambda: FakeConn())

        r = client.get("/chat/uploads/recent/for-restoration?current_thread_id=t-current")
        assert r.status_code == 200
        names = [u["filename"] for u in r.json()["uploads"]]
        assert "elsewhere.pdf" in names
        assert "onthread.pdf" not in names, (
            "Uploads already on the current thread must not appear in the "
            "restoration banner — those aren't 'lost' from the user's POV."
        )

    def test_empty_catalog_returns_zero(self, client, monkeypatch):
        class FakeCur:
            def execute(self, sql, params=None): pass
            def fetchall(self): return []
            def close(self): pass
        class FakeConn:
            def cursor(self): return FakeCur()
            def close(self): pass
        monkeypatch.setattr("app.storage.instant_rag_catalog._conn", lambda: FakeConn())

        r = client.get("/chat/uploads/recent/for-restoration")
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_auth_required_scopes_to_user(self, client, monkeypatch):
        """When auth=required and a JWT is present, use list_for_user
        (catalog scopes to that user_id). Validates the router's
        dependency plumbing."""
        monkeypatch.setenv("CHAT_AUTH_MODE", "required")
        from app.api import uploads as uploads_mod

        with patch("app.auth.get_user_id_from_request", return_value="u-42"), \
             patch.object(uploads_mod, "list_for_user") as list_mock:
            list_mock.return_value = [_sample_upload_row(doc_id="doc-x", user_id="u-42")]
            r = client.get(
                "/chat/uploads/recent/for-restoration",
                headers={"Authorization": "Bearer good"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["auth_scope"] == "user"
        assert body["count"] == 1
        list_mock.assert_called_once()

    def test_auth_required_without_token_returns_401(self, client, monkeypatch):
        """The require_user dependency must gate this endpoint when
        auth=required. Otherwise one user could see another user's
        upload catalog."""
        monkeypatch.setenv("CHAT_AUTH_MODE", "required")
        r = client.get("/chat/uploads/recent/for-restoration")
        assert r.status_code == 401


# ── POST /chat/uploads/{document_id}/link-to-thread ──────────────────────


class TestLinkUploadToThread:
    def test_happy_path_links_upload(self, client, monkeypatch):
        from app.api import uploads as uploads_mod

        with patch.object(uploads_mod, "get_by_document_id",
                          return_value=_sample_upload_row(doc_id="doc-1",
                                                           thread_id="t-origin",
                                                           filename="Sunshine.pdf")), \
             patch("app.storage.threads.ensure_thread", return_value="t-target"), \
             patch("app.storage.threads.append_uploaded_file_record",
                   return_value=True) as append_mock:
            r = client.post(
                "/chat/uploads/doc-1/link-to-thread",
                json={"thread_id": "t-target"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["linked"] is True
        assert body["document_id"] == "doc-1"
        assert body["target_thread_id"] == "t-target"
        assert body["origin_thread_id"] == "t-origin"

        # Verify the record written into active.uploaded_files[] carries
        # the right shape so _resolve_upload_document_id finds it.
        append_mock.assert_called_once()
        (tid_arg, record_arg), _kwargs = append_mock.call_args
        assert tid_arg == "t-target"
        assert record_arg["document_id"] == "doc-1"
        assert record_arg["purpose"] == "instant_rag"
        assert record_arg["filename"] == "Sunshine.pdf"
        # "linked_from_thread" marker should be set so future UIs can
        # distinguish original vs linked records.
        assert record_arg.get("linked_from_thread") == "t-origin"

    def test_missing_thread_id_400(self, client, monkeypatch):
        from app.api import uploads as uploads_mod
        with patch.object(uploads_mod, "get_by_document_id",
                          return_value=_sample_upload_row()):
            r = client.post("/chat/uploads/doc-1/link-to-thread", json={})
        assert r.status_code == 400

    def test_unknown_document_id_404(self, client, monkeypatch):
        from app.api import uploads as uploads_mod
        with patch.object(uploads_mod, "get_by_document_id", return_value=None):
            r = client.post(
                "/chat/uploads/doc-nope/link-to-thread",
                json={"thread_id": "t-target"},
            )
        assert r.status_code == 404

    def test_inactive_upload_409(self, client, monkeypatch):
        """Linking an expired/discarded/promoted upload makes no sense —
        the catalog row is there but the chunks may be gone."""
        from app.api import uploads as uploads_mod
        with patch.object(uploads_mod, "get_by_document_id",
                          return_value=_sample_upload_row(status="expired")):
            r = client.post(
                "/chat/uploads/doc-1/link-to-thread",
                json={"thread_id": "t-target"},
            )
        assert r.status_code == 409

    def test_ownership_enforced_when_auth_required(self, client, monkeypatch):
        """When auth=required and the upload's user_id doesn't match the
        caller, return 403. Otherwise user A could steal user B's
        uploads into their own thread."""
        monkeypatch.setenv("CHAT_AUTH_MODE", "required")
        from app.api import uploads as uploads_mod

        with patch("app.auth.get_user_id_from_request", return_value="u-attacker"), \
             patch.object(uploads_mod, "get_by_document_id",
                          return_value=_sample_upload_row(user_id="u-owner")):
            r = client.post(
                "/chat/uploads/doc-1/link-to-thread",
                json={"thread_id": "t-target"},
                headers={"Authorization": "Bearer attacker-token"},
            )
        assert r.status_code == 403
        assert "another user" in r.json()["detail"].lower()

    def test_ownership_passes_when_auth_required_and_matches(self, client, monkeypatch):
        monkeypatch.setenv("CHAT_AUTH_MODE", "required")
        from app.api import uploads as uploads_mod

        with patch("app.auth.get_user_id_from_request", return_value="u-42"), \
             patch.object(uploads_mod, "get_by_document_id",
                          return_value=_sample_upload_row(user_id="u-42")), \
             patch("app.storage.threads.ensure_thread", return_value="t-target"), \
             patch("app.storage.threads.append_uploaded_file_record", return_value=True):
            r = client.post(
                "/chat/uploads/doc-1/link-to-thread",
                json={"thread_id": "t-target"},
                headers={"Authorization": "Bearer owner-token"},
            )
        assert r.status_code == 200


# ── Frontend structural guards ───────────────────────────────────────────


class TestFrontendBannerMarkup:
    """Anchor IDs the restore-banner JS relies on must exist in the
    served index.html. If the HTML regresses to remove them, the JS
    null-checks leave the banner dead without a visible error."""

    def test_banner_anchors_present(self):
        from pathlib import Path
        html = Path(__file__).parent.parent / "frontend" / "index.html"
        text = html.read_text()
        for anchor in (
            'id="uploadRestoreBanner"',
            'id="uploadRestoreBannerList"',
            'id="uploadRestoreBannerDismiss"',
        ):
            assert anchor in text, (
                f"Restoration banner anchor {anchor} missing. JS relies on "
                f"these IDs to show/hide and populate the banner."
            )

    def test_banner_wiring_in_ts_source(self):
        from pathlib import Path
        ts = Path(__file__).parent.parent / "frontend" / "src" / "app.ts"
        text = ts.read_text()
        for symbol in (
            "maybeShowRestoreBanner",
            "linkUploadToCurrentThread",
            "/chat/uploads/recent/for-restoration",
            "/link-to-thread",
            "_mobiusRestoreBannerDismissed",  # sessionStorage key
        ):
            assert symbol in text, (
                f"TS source missing restore-banner identifier {symbol!r} — "
                f"the banner won't function."
            )

    def test_bundled_js_contains_restore_logic(self):
        """Ensure `npm run build` ran after the TS edit."""
        from pathlib import Path
        js = Path(__file__).parent.parent / "frontend" / "static" / "app.js"
        text = js.read_text()
        assert "/chat/uploads/recent/for-restoration" in text, (
            "Built bundle doesn't contain the restore-banner fetch URL. "
            "Rebuild with: cd frontend && npm run build"
        )
