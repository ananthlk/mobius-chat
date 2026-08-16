"""verify_claim scaffold — resolve → page-text → [judge hole] → verdict.

Scaffold contract (DOWNLOAD_AGENT_CORE_REQUIREMENTS.md §8.4/§8.5):
- document_id resolved by PK only, no fuzzy fallback (§6b);
- page text pulled from RAG /documents/{id}/pages;
- verdict JUDGMENT is a pluggable injection point (Eval's retrieval_grade);
- until the judge is wired, every verify is LOUD: verdict=low_coverage,
  status="judge_unwired" — never a silent/false agree;
- when a judge is injected, its verdict flows through and status="ok";
- page_text_chars proves the substrate ran regardless of the judge.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.services.claim_verification as cv
from app.api.front_door import require_user


_DOC_ID = "b5e32506-26d5-4d42-a8b8-4561bc788027"
_ROW = {"document_id": _DOC_ID, "document_filename": "attachment_ii.pdf"}
_PAGE80 = "...an enrollee may file a plan appeal orally or in writing within sixty (60) calendar days..."


@pytest.fixture(autouse=True)
def _unwire_judge():
    # Every test starts with the judge unwired (module-global); restore after.
    cv.set_judge(None)
    yield
    cv.set_judge(None)


def _patch_substrate(monkeypatch, row=_ROW, page_text=_PAGE80, resolved_page=80):
    import app.skills.builtin.fetch_document as fd
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(row) if row else None)
    monkeypatch.setattr(cv, "_fetch_page_text", lambda did, page: (page_text, resolved_page))


# ── core function ───────────────────────────────────────────────────


def test_unwired_judge_is_loud_low_coverage(monkeypatch):
    _patch_substrate(monkeypatch)
    out = cv.verify_claim(_DOC_ID, "enrollee plan appeal deadline is 60 calendar days", page=80)
    assert out["verdict"] == "low_coverage"
    assert out["status"] == "judge_unwired"      # LOUD — not a false agree
    assert out["document_id"] == _DOC_ID
    assert out["page"] == 80
    assert out["page_text_chars"] == len(_PAGE80)  # substrate ran


def test_injected_judge_verdict_flows_through(monkeypatch):
    _patch_substrate(monkeypatch)
    seen = {}
    def _judge(claim, page_text):
        seen["claim"] = claim
        seen["text"] = page_text
        return {"verdict": "agree", "quote": "within sixty (60) calendar days"}
    cv.set_judge(_judge)

    out = cv.verify_claim(_DOC_ID, "enrollee plan appeal deadline is 60 calendar days", page=80)
    assert out["verdict"] == "agree"
    assert out["status"] == "ok"
    assert out["quote"] == "within sixty (60) calendar days"
    assert seen["text"] == _PAGE80  # the judge saw the real page text


def test_judge_bad_verdict_coerced_to_low_coverage(monkeypatch):
    _patch_substrate(monkeypatch)
    cv.set_judge(lambda c, t: {"verdict": "definitely-yes", "quote": "x"})
    out = cv.verify_claim(_DOC_ID, "claim", page=80)
    assert out["verdict"] == "low_coverage"  # unknown enum never leaks out


def test_document_not_found_is_clean(monkeypatch):
    _patch_substrate(monkeypatch, row=None)
    out = cv.verify_claim(_DOC_ID, "claim", page=80)
    assert out["status"] == "document_not_found"
    assert out["verdict"] == "low_coverage"


def test_no_page_text_is_clean(monkeypatch):
    _patch_substrate(monkeypatch, page_text="", resolved_page=None)
    out = cv.verify_claim(_DOC_ID, "claim", page=80)
    assert out["status"] == "no_page_text"


def test_page_fetch_error_is_caught(monkeypatch):
    import app.skills.builtin.fetch_document as fd
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: dict(_ROW))
    def _boom(did, page):
        raise RuntimeError("rag down")
    monkeypatch.setattr(cv, "_fetch_page_text", _boom)
    out = cv.verify_claim(_DOC_ID, "claim", page=80)
    assert out["status"] == "page_fetch_error"


@pytest.mark.parametrize("doc_id,claim", [("", "c"), (_DOC_ID, ""), ("", "")])
def test_bad_request_on_empty_inputs(doc_id, claim):
    out = cv.verify_claim(doc_id, claim)
    assert out["status"] == "bad_request"


def test_no_fuzzy_fallback_only_resolve_by_id(monkeypatch):
    # PK-only per §6b: verify_claim must call _resolve_by_id and NOTHING
    # fuzzy (no _fetch_candidates / _rank_matches / web registry).
    import app.skills.builtin.fetch_document as fd
    called = {}
    monkeypatch.setattr(fd, "_resolve_by_id", lambda did: (called.__setitem__("id", did) or dict(_ROW)))
    for fuzzy in ("_fetch_candidates", "_corpus_search_resolve", "_web_registry_resolve", "_rank_matches"):
        monkeypatch.setattr(fd, fuzzy, lambda *a, **k: (_ for _ in ()).throw(AssertionError(f"{fuzzy} called")))
    monkeypatch.setattr(cv, "_fetch_page_text", lambda did, page: (_PAGE80, 80))

    cv.verify_claim(_DOC_ID, "claim", page=80)
    assert called["id"] == _DOC_ID


# ── HTTP endpoint ───────────────────────────────────────────────────


def _client(monkeypatch):
    _patch_substrate(monkeypatch)
    import app.api.verify_claim as vc
    app = FastAPI()
    app.include_router(vc.router)
    app.dependency_overrides[require_user] = lambda: "cert-sweep"
    return TestClient(app)


def test_endpoint_returns_verdict_schema(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/chat/verify-claim", json={"document_id": _DOC_ID, "claim": "60 calendar days", "page": 80})
    assert r.status_code == 200
    body = r.json()
    assert set(["verdict", "quote", "page", "document_id", "status"]).issubset(body)
    assert body["status"] == "judge_unwired"


def test_endpoint_reflects_injected_judge(monkeypatch):
    c = _client(monkeypatch)
    cv.set_judge(lambda claim, text: {"verdict": "contradict", "quote": "ninety (90) days"})
    r = c.post("/chat/verify-claim", json={"document_id": _DOC_ID, "claim": "deadline is 60 days", "page": 80})
    assert r.json()["verdict"] == "contradict"
    assert r.json()["status"] == "ok"
