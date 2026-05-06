"""Phase 1a — extracted /chat/history router.

First slice of the main-split refactor. These tests assert:
1. The router is mountable and its URLs match the original main.py paths
   (back-compat for every FE and API client).
2. Endpoints delegate to the same ``app.storage`` functions as before
   (no behavioral drift during extraction).
3. The limit-parse helper behaves identically.
4. No new endpoint accidentally landed back in main.py during the move.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient


def _app():
    """Build a tiny FastAPI app that mounts only the extracted router.

    Avoids importing the full ``app.main`` (which pulls in Vertex, DB
    connections, etc.) so these tests are fast and hermetic.
    """
    from fastapi import FastAPI

    from app.api.history import router

    a = FastAPI()
    a.include_router(router)
    return a


# ── URL surface: back-compat with the pre-1a main.py paths ─────────────────


class TestURLBackCompat:
    """The external URL surface MUST NOT change during the main-split.

    Every path that used to work under ``@app.get("/chat/history/...")``
    in main.py must still work after extraction. Regressions here break
    the FE and every external client.
    """

    def test_recent_path_exists(self):
        client = TestClient(_app())
        with patch("app.api.history.get_recent_turns", return_value=[]):
            r = client.get("/chat/history/recent")
        assert r.status_code == 200

    def test_threads_path_exists(self):
        client = TestClient(_app())
        with patch("app.storage.threads.get_recent_threads", return_value=[]):
            r = client.get("/chat/history/threads")
        assert r.status_code == 200

    def test_most_helpful_searches_path_exists(self):
        client = TestClient(_app())
        with patch("app.api.history.get_most_helpful_turns", return_value=[]):
            r = client.get("/chat/history/most-helpful-searches")
        assert r.status_code == 200

    def test_most_helpful_documents_path_exists(self):
        client = TestClient(_app())
        with patch("app.api.history.get_most_helpful_documents", return_value=[]):
            r = client.get("/chat/history/most-helpful-documents")
        assert r.status_code == 200


# ── Delegation: endpoints call the same storage fns with the same args ─────


class TestDelegation:
    def test_recent_delegates_with_limit(self):
        client = TestClient(_app())
        with patch("app.api.history.get_recent_turns", return_value=[]) as m:
            client.get("/chat/history/recent?limit=25")
        m.assert_called_once_with(25, user_id=None)

    def test_threads_delegates_with_limit(self):
        client = TestClient(_app())
        with patch(
            "app.storage.threads.get_recent_threads", return_value=[]
        ) as m:
            client.get("/chat/history/threads?limit=15")
        m.assert_called_once_with(15, user_id=None)

    def test_most_helpful_turns_delegates_with_limit(self):
        client = TestClient(_app())
        with patch(
            "app.api.history.get_most_helpful_turns", return_value=[]
        ) as m:
            client.get("/chat/history/most-helpful-searches?limit=3")
        m.assert_called_once_with(3, user_id=None)

    def test_most_helpful_documents_delegates_with_limit(self):
        client = TestClient(_app())
        with patch(
            "app.api.history.get_most_helpful_documents", return_value=[]
        ) as m:
            client.get("/chat/history/most-helpful-documents?limit=50")
        m.assert_called_once_with(50, user_id=None)


# ── _parse_limit behavior preserved ────────────────────────────────────────


class TestParseLimit:
    def test_default_is_ten(self):
        from app.api.history import _parse_limit

        assert _parse_limit(None) == 10

    def test_respects_upper_cap(self):
        from app.api.history import _parse_limit

        assert _parse_limit(10_000) == 100

    def test_respects_lower_bound(self):
        from app.api.history import _parse_limit

        assert _parse_limit(-5) == 1

    def test_passes_in_range(self):
        from app.api.history import _parse_limit

        assert _parse_limit(37) == 37


# ── main.py hygiene: no ``@app.get("/chat/history/...")`` left behind ──────


class TestMainPyHygiene:
    """Prevent regression: new /chat/history endpoints must go in the router.

    If someone adds ``@app.get("/chat/history/new")`` back to main.py this
    test fails, reminding them to use the extracted router.
    """

    def test_no_chat_history_decorator_in_main_py(self):
        from pathlib import Path

        main_py = Path(__file__).parent.parent / "app" / "main.py"
        text = main_py.read_text()
        # Allow the include_router line but forbid any direct decorator.
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("@app.get") and "/chat/history" in line:
                raise AssertionError(
                    f"/chat/history endpoint still decorated in main.py — "
                    f"move to app/api/history.py:\n  {line}"
                )
