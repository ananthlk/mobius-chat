"""A storage failure must not be reported as a PHI verdict.

Observed live 2026-09-09 (mobius-chat-00975-bpc): the classifier ruled a
document CLEAN, rag's /publish threw a 500 on a non-idempotent republish
(rag_published_embeddings_pkey), and the gate's catch-all reported
`blocked_indeterminate`. The browser-extension seat then spent an hour chasing a
PHI gate regression that was a storage bug, because the surface told them PHI.

`indeterminate` is a CLASSIFIER answer — "I could not decide". Reusing it for
"the pipeline exploded after a clean verdict" makes those two indistinguishable
in the one surface where the distinction is the entire point.

THE SAFETY PROPERTY IS UNCHANGED AND THESE TESTS EXIST TO KEEP IT THAT WAY: a
publish failure still BLOCKS. Nothing here relaxes fail-closed. The document is
described accurately, not admitted.
"""
from __future__ import annotations

import pytest


def _verdict(gate="clean", phi=False):
    return {"gate": gate, "phi_flag": phi, "identifier_labels": [],
            "transaction_id": "txn-1", "classifier_version": "v9",
            "layers_run": ["regex", "ner"], "confidence": 0.98}


def _upload_with_gate_raising(m, exc, monkeypatch):
    """Drive the REAL caller — _handle_instant_rag_upload's try/except — rather
    than a copy of its logic. A test that reimplements the code under test can
    pass while the code is broken; that is the failure mode this whole lane has
    been about, and it would be absurd to reproduce it here.
    """
    def _boom(**kw):
        raise exc
    monkeypatch.setattr(m, "_run_hipaa_gate_sync", _boom)
    monkeypatch.setattr(m, "_resolve_gate_org", lambda uid: ("org", "src"))
    monkeypatch.setenv("PHI_CLASSIFIER_URL", "http://phi.test")

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"document_id":"d1","estimated_processing_seconds":1}'

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
    return m._handle_instant_rag_upload(
        content=b"hello", filename="f.txt", org_name="Acme",
        thread_id=None, file_purpose="instant_rag", user_id="u-1",
    )


def test_publish_failure_still_blocks(monkeypatch):
    """The safety property. If this ever fails, the change was not narrow."""
    import app.main as m
    res = _upload_with_gate_raising(
        m, m.GatePublishFailed(_verdict(), RuntimeError("HTTP Error 500")), monkeypatch)
    assert res.get("blocked") is True, "fail-closed was weakened"
    assert res.get("status") == "blocked"


def test_publish_failure_reports_storage_not_phi(monkeypatch):
    import app.main as m
    res = _upload_with_gate_raising(
        m, m.GatePublishFailed(_verdict(gate="clean", phi=False),
                               RuntimeError("HTTP Error 500")), monkeypatch)
    d = res.get("hipaa_diagnostics") or {}
    assert res.get("blocked") is True
    assert d.get("gate") == "clean", "the classifier's real answer was overwritten"
    assert d.get("phi_flag") is False, "a clean document was flagged as PHI"


def test_other_failures_still_report_indeterminate(monkeypatch):
    """An audit-write failure cannot prove a verdict was reached, so
    `indeterminate` is honest there and must not change."""
    import app.main as m
    res = _upload_with_gate_raising(m, RuntimeError("audit write failed"), monkeypatch)
    d = res.get("hipaa_diagnostics") or {}
    assert res.get("blocked") is True
    assert d.get("gate") == "indeterminate"
    assert d.get("phi_flag") is True


# ── fail-closed on misconfiguration (PHI-classifier seat's ruling) ───
# Before: an unset PHI_CLASSIFIER_URL logged at DEBUG and left gate_result
# empty, so `blocked` was falsy and the document published UNSCREENED with no
# audit row. A deploy that forgot the env var would silently publish PHI, and
# the only trace was a debug line production does not emit.

def test_unset_classifier_url_blocks_instead_of_publishing(monkeypatch):
    import app.main as m
    monkeypatch.delenv("PHI_CLASSIFIER_URL", raising=False)

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"document_id":"d1","estimated_processing_seconds":1}'

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
    res = m._handle_instant_rag_upload(
        content=b"hello", filename="f.txt", org_name="Acme",
        thread_id=None, file_purpose="instant_rag", user_id="u-1",
    )
    assert res.get("blocked") is True, "unscreened document was published"
    assert res.get("status") == "blocked"
    d = res.get("hipaa_diagnostics") or {}
    # Distinct from blocked_indeterminate and blocked_publish_failed: three
    # different causes must not share one word.
    assert d.get("action_taken") == "blocked_unconfigured"


def test_misconfiguration_does_not_tell_the_user_to_retry(monkeypatch):
    """Retrying cannot succeed — the service is unconfigured until someone
    changes a deploy variable. 'Try again shortly' is a plausible, wrong answer
    of exactly the kind this gate keeps producing."""
    import app.main as m
    monkeypatch.delenv("PHI_CLASSIFIER_URL", raising=False)

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"document_id":"d1","estimated_processing_seconds":1}'

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
    res = m._handle_instant_rag_upload(
        content=b"x", filename="policy.pdf", org_name="Acme",
        thread_id=None, file_purpose="instant_rag", user_id="u-1",
    )
    msg = res.get("message") or ""
    assert "try again" not in msg.lower()
    assert "administrator" in msg.lower()


# ── source_origin: origin ONLY, never the full URL ───────────────────

def test_origin_strips_the_parts_where_phi_hides():
    """A path or query is exactly where PHI lives in a URL. Origin-only is what
    keeps this consistent with not widening the classifier payload."""
    from app.main import _origin_of
    assert _origin_of("https://www.molinahealthcare.com/members/1234567/claims?patient=abc") \
        == "https://www.molinahealthcare.com"
    assert _origin_of("http://x.test:8080/a/b?c=d") == "http://x.test:8080"


def test_origin_is_empty_rather_than_wrong_for_junk():
    from app.main import _origin_of
    for junk in ("", None, "notaurl", "ftp://x/y", "javascript:alert(1)"):
        assert _origin_of(junk) == ""
