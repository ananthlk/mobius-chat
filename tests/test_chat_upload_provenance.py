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
