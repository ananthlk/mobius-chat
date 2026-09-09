"""TODO-B — /chat/upload must not silently discard the user-fetch provenance.

The browser-extension lane has been sending source_url / access / task_id /
fetched_at / signal_headers on every upload. None were declared as Form fields,
and an undeclared Form field in FastAPI is not an error — it is simply absent.
So the upload SUCCEEDED, returned 200, and the provenance evaporated with no log
to show for it. Nothing on either side could tell that from working correctly.

The second half matters as much: rag's /upload declares `source_url` but has no
parameter for the other four (mobius-rag/app/main.py:8076-8086). Forwarding them
anyway would have FastAPI discard them there instead — the same bug moved one hop
downstream, which is not a fix. So they are surfaced in the response as pending
rather than sent into a void.
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

import app.main as m


def _post(**extra):
    captured = {}

    def _fake_handle(**kw):
        captured.update(kw)
        return {"document_id": "doc-1", "status": "ok"}

    with patch.object(m, "_handle_instant_rag_upload", _fake_handle), \
         patch.object(m, "require_user", lambda: "u-1"):
        m.app.dependency_overrides[m.require_user] = lambda: "u-1"
        try:
            c = TestClient(m.app)
            r = c.post("/chat/upload",
                       files={"file": ("policy.pdf", b"%PDF-1.4 test", "application/pdf")},
                       data={"thread_id": "t-1", "org_name": "Aetna", **extra})
        finally:
            m.app.dependency_overrides.pop(m.require_user, None)
    return r, captured


def test_user_fetch_fields_are_not_dropped():
    r, cap = _post(
        source_url="https://www.aetna.com/cpb/0330.html",
        access="user-fetch",
        task_id="task-abc123",
        fetched_at="2026-09-09T20:00:00Z",
        signal_headers='{"content-type":"text/html"}',
    )
    assert r.status_code == 200, r.text
    assert cap.get("source_url") == "https://www.aetna.com/cpb/0330.html"
    prov = cap.get("fetch_provenance") or {}
    assert prov.get("access") == "user-fetch"
    assert prov.get("task_id") == "task-abc123"
    assert prov.get("fetched_at") == "2026-09-09T20:00:00Z"
    assert prov.get("signal_headers")


def test_absent_fields_stay_absent_rather_than_becoming_empty_strings():
    """A blank provenance key is worse than a missing one — it reads as
    'the sender said nothing' instead of 'the sender sent nothing'."""
    r, cap = _post()
    assert r.status_code == 200
    assert cap.get("source_url") is None
    assert cap.get("fetch_provenance") == {}


# ── the hop shapes differ, and that is the whole trap ────────────────
# The extension sends these to chat as multipart FORM fields; rag declares them
# as QUERY params (verified against rag's DEPLOYED OpenAPI — a local source read
# was stale). Forwarding them in the inbound shape makes rag return 200 and drop
# all five: the same silent drop, relocated to the last hop, after both sides
# believe they are done.

def test_provenance_is_forwarded_to_rag_on_the_QUERY_STRING():
    import app.main as m
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"document_id":"d1","status":"published"}'

    def _fake_urlopen(req, timeout=None):
        # urlopen is shared — db_client's MCP hop uses it too. Capture only the
        # rag upload, or an unrelated call overwrites what we came to assert.
        if "/upload" in req.full_url:
            captured["url"] = req.full_url
            captured["body"] = req.data
        return _Resp()

    with patch("urllib.request.urlopen", _fake_urlopen), \
         patch.dict("os.environ", {"MOBIUS_RAG_URL": "http://rag.test"}):
        try:
            m._handle_instant_rag_upload(
                content=b"%PDF-1.4", filename="p.pdf", org_name="Aetna",
                thread_id="t-1", file_purpose="instant_rag", user_id="u-1",
                source_url="https://aetna.com/cpb/0330.html",
                fetch_provenance={
                    "access": "user_authorized_session",
                    "task_id": "task-abc",
                    "fetched_at": "2026-09-09T20:00:00Z",
                    "signal_headers": "x-robots-tag: noai",
                },
            )
        except Exception:
            pass  # downstream bookkeeping is not what this test is about

    url = captured.get("url", "")
    assert url, "rag was never called"
    for key in ("source_url", "access", "task_id", "fetched_at", "signal_headers"):
        assert f"{key}=" in url, f"{key} was not on the query string — rag will drop it"
    # and must NOT have been smuggled into the multipart body instead
    body = captured.get("body") or b""
    assert b'name="access"' not in body, "forwarded as a form field; rag ignores those"


def test_access_is_forwarded_verbatim_not_validated_here():
    """rag maps `access` through a closed map and 422s on an unknown value
    rather than defaulting the classification caller. Copying that map into chat
    would duplicate a RULE, and a duplicated rule drifts — rag owns the answer."""
    import inspect
    import app.main as m
    src = inspect.getsource(m._handle_instant_rag_upload)
    assert "user_authorized_session" not in src, (
        "chat is validating `access` itself — that duplicates rag's closed map")
