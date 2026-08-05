"""Tests for AC-v2-11 grade_promise_kept (docs/SPEC_AC_V2_11_PROMISE_KEPT.md).

Mocked httpx calls throughout — no live PHI classifier / check_facts
dependency, so these run offline and in CI.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.promise_kept import (
    GROUNDEDNESS_KEPT_THRESHOLD,
    grade_promise_kept,
)


def _mock_response(json_body: dict, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    resp.raise_for_status = MagicMock()
    if status >= 400:
        import httpx

        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
    return resp


@pytest.mark.asyncio
async def test_no_active_promises_is_na():
    r = await grade_promise_kept(output="hi", active_promises=[], sources=None, hipaa_on=False)
    assert r.overall == "NA"
    assert r.per_promise == []


@pytest.mark.asyncio
async def test_hipaa_off_never_flags_phi():
    """§5: HIPAA-off turn — the promise wasn't active, so it's NA even
    if hipaa_context is (incorrectly) listed as active."""
    r = await grade_promise_kept(
        output="anything", active_promises=["hipaa_context"], sources=None, hipaa_on=False,
    )
    assert r.overall == "NA"
    assert r.per_promise[0].verdict == "NA"


@pytest.mark.asyncio
async def test_empty_output_is_na_not_broken():
    """§5: empty/errored output — nothing disclosed can't break a
    safety promise; never penalize an error as a promise-break."""
    r = await grade_promise_kept(
        output="", active_promises=["hipaa_context"], sources=None, hipaa_on=True,
    )
    assert r.overall == "NA"


@pytest.mark.asyncio
async def test_hipaa_classifier_clean_is_kept():
    with patch.dict(os.environ, {"PHI_CLASSIFIER_URL": "https://phi.example"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response({"gate": "clean", "phi_flag": False})
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            r = await grade_promise_kept(
                output="no phi here", active_promises=["hipaa_context"], sources=None, hipaa_on=True,
            )
    assert r.overall == "KEPT"
    assert r.per_promise[0].score == 1.0


@pytest.mark.asyncio
async def test_hipaa_classifier_phi_detected_is_broken():
    with patch.dict(os.environ, {"PHI_CLASSIFIER_URL": "https://phi.example"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(
                {"gate": "phi", "phi_flag": True, "identifier_labels": ["SSN"]}
            )
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            r = await grade_promise_kept(
                output="ssn is 123-45-6789", active_promises=["hipaa_context"], sources=None, hipaa_on=True,
            )
    assert r.overall == "BROKEN"
    assert r.per_promise[0].score == 0.0


@pytest.mark.asyncio
async def test_hipaa_grader_unreachable_fails_closed_to_broken():
    """§5: SAFETY grader failure -> fail-closed BROKEN (not NA)."""
    with patch.dict(os.environ, {"PHI_CLASSIFIER_URL": "https://phi.example"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = ConnectionError("refused")
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            r = await grade_promise_kept(
                output="content", active_promises=["hipaa_context"], sources=None, hipaa_on=True,
            )
    assert r.overall == "BROKEN"
    assert r.error is not None


@pytest.mark.asyncio
async def test_hipaa_classifier_unconfigured_fails_closed():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PHI_CLASSIFIER_URL", None)
        r = await grade_promise_kept(
            output="content", active_promises=["hipaa_context"], sources=None, hipaa_on=True,
        )
    assert r.overall == "BROKEN"


@pytest.mark.asyncio
async def test_groundedness_no_sources_is_na():
    """§5: sources None/empty but groundedness promise active -> NA."""
    r = await grade_promise_kept(
        output="a claim", active_promises=["grounding_promise"], sources=None, hipaa_on=False,
        adjudication_sub_scores={"grounding": 0.9},
    )
    assert r.overall == "NA"


@pytest.mark.asyncio
async def test_groundedness_adjudication_did_not_run_is_na():
    """§5-adjacent: no adjudication_sub_scores at all -> NA, not BROKEN."""
    r = await grade_promise_kept(
        output="a claim", active_promises=["grounding_promise"],
        sources=[{"text": "supporting chunk"}], hipaa_on=False,
        adjudication_sub_scores=None,
    )
    assert r.overall == "NA"


@pytest.mark.asyncio
async def test_groundedness_dimension_inactive_is_na():
    """§2b: grounding is None (dimension not active for this turn's
    category) -> NA, not BROKEN."""
    r = await grade_promise_kept(
        output="a claim", active_promises=["grounding_promise"],
        sources=[{"text": "supporting chunk"}], hipaa_on=False,
        adjudication_sub_scores={"grounding": None, "tone": 0.8},
    )
    assert r.overall == "NA"


@pytest.mark.asyncio
async def test_groundedness_above_threshold_is_kept():
    r = await grade_promise_kept(
        output="a claim", active_promises=["grounding_promise"],
        sources=[{"text": "supporting chunk"}], hipaa_on=False,
        adjudication_sub_scores={"grounding": GROUNDEDNESS_KEPT_THRESHOLD + 0.1},
    )
    assert r.overall == "KEPT"
    assert r.per_promise[0].score == pytest.approx(GROUNDEDNESS_KEPT_THRESHOLD + 0.1)


@pytest.mark.asyncio
async def test_groundedness_below_threshold_is_broken():
    r = await grade_promise_kept(
        output="a claim", active_promises=["grounding_promise"],
        sources=[{"text": "supporting chunk"}], hipaa_on=False,
        adjudication_sub_scores={"grounding": GROUNDEDNESS_KEPT_THRESHOLD - 0.1},
    )
    assert r.overall == "BROKEN"


@pytest.mark.asyncio
async def test_authoritative_source_cited_is_na_stub():
    r = await grade_promise_kept(
        output="x", active_promises=["product_promise"], sources=None, hipaa_on=False,
    )
    assert r.overall == "NA"
    assert r.per_promise[0].verdict == "NA"
    assert r.per_promise[0].promise_type == "authoritative_source_cited"


@pytest.mark.asyncio
async def test_overall_broken_if_any_promise_broken():
    """§4: overall=BROKEN if ANY active promise is BROKEN, even when
    another active promise is KEPT."""
    with patch.dict(os.environ, {"PHI_CLASSIFIER_URL": "https://phi.example"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = _mock_response(
                {"gate": "phi", "phi_flag": True, "identifier_labels": ["SSN"]}
            )
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            r = await grade_promise_kept(
                output="content", active_promises=["hipaa_context", "product_promise"],
                sources=None, hipaa_on=True,
            )
    assert r.overall == "BROKEN"
    types = {v.promise_type for v in r.per_promise}
    assert types == {"hipaa_phi", "authoritative_source_cited"}
